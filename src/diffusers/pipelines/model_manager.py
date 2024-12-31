# Copyright 2024 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import OrderedDict
from itertools import combinations
from typing import List, Optional, Union

import torch

from ..utils import (
    is_accelerate_available,
    logging,
)


if is_accelerate_available():
    from accelerate.hooks import ModelHook, add_hook_to_module, remove_hook_from_module
    from accelerate.state import PartialState
    from accelerate.utils import send_to_device
    from accelerate.utils.memory import clear_device_cache

logger = logging.get_logger(__name__)  # pylint: disable=invalid-name


# YiYi Notes: copied from modeling_utils.py (decide later where to put this)
def get_memory_footprint(self, return_buffers=True):
    r"""
    Get the memory footprint of a model. This will return the memory footprint of the current model in bytes. Useful to
    benchmark the memory footprint of the current model and design some tests. Solution inspired from the PyTorch
    discussions: https://discuss.pytorch.org/t/gpu-memory-that-model-uses/56822/2

    Arguments:
        return_buffers (`bool`, *optional*, defaults to `True`):
            Whether to return the size of the buffer tensors in the computation of the memory footprint. Buffers are
            tensors that do not require gradients and not registered as parameters. E.g. mean and std in batch norm
            layers. Please see: https://discuss.pytorch.org/t/what-pytorch-means-by-buffers/120266/2
    """
    mem = sum([param.nelement() * param.element_size() for param in self.parameters()])
    if return_buffers:
        mem_bufs = sum([buf.nelement() * buf.element_size() for buf in self.buffers()])
        mem = mem + mem_bufs
    return mem


class CustomOffloadHook(ModelHook):
    """
    A hook that offloads a model on the CPU until its forward pass is called. It ensures the model and its inputs are
    on the given device. Optionally offloads other models to the CPU before the forward pass is called.

    Args:
        execution_device(`str`, `int` or `torch.device`, *optional*):
            The device on which the model should be executed. Will default to the MPS device if it's available, then
            GPU 0 if there is a GPU, and finally to the CPU.
    """

    def __init__(
        self,
        execution_device: Optional[Union[str, int, torch.device]] = None,
        other_hooks: Optional[List["UserCustomOffloadHook"]] = None,
        offload_strategy: Optional["AutoOffloadStrategy"] = None,
    ):
        self.execution_device = execution_device if execution_device is not None else PartialState().default_device
        self.other_hooks = other_hooks
        self.offload_strategy = offload_strategy
        self.model_id = None

    def set_strategy(self, offload_strategy: "AutoOffloadStrategy"):
        self.offload_strategy = offload_strategy

    def add_other_hook(self, hook: "UserCustomOffloadHook"):
        """
        Add a hook to the list of hooks to consider for offloading.
        """
        if self.other_hooks is None:
            self.other_hooks = []
        self.other_hooks.append(hook)

    def init_hook(self, module):
        return module.to("cpu")

    def pre_forward(self, module, *args, **kwargs):
        if module.device != self.execution_device:
            if self.other_hooks is not None:
                hooks_to_offload = [hook for hook in self.other_hooks if hook.model.device == self.execution_device]
                # offload all other hooks
                import time

                # YiYi Notes: only logging time for now to monitor the overhead of offloading strategy (remove later)
                start_time = time.perf_counter()
                if self.offload_strategy is not None:
                    hooks_to_offload = self.offload_strategy(
                        hooks=hooks_to_offload,
                        model_id=self.model_id,
                        model=module,
                        execution_device=self.execution_device,
                    )
                end_time = time.perf_counter()
                logger.info(
                    f" time taken to apply offload strategy for {self.model_id}: {(end_time - start_time):.2f} seconds"
                )

                for hook in hooks_to_offload:
                    logger.info(
                        f"moving {self.model_id} to {self.execution_device}, offloading {hook.model_id} to cpu"
                    )
                    hook.offload()

                if hooks_to_offload:
                    clear_device_cache()
            module.to(self.execution_device)
        return send_to_device(args, self.execution_device), send_to_device(kwargs, self.execution_device)


class UserCustomOffloadHook:
    """
    A simple hook grouping a model and a `CustomOffloadHook`, which provides easy APIs for to call the init method of
    the hook or remove it entirely.
    """

    def __init__(self, model_id, model, hook):
        self.model_id = model_id
        self.model = model
        self.hook = hook

    def offload(self):
        self.hook.init_hook(self.model)

    def attach(self):
        add_hook_to_module(self.model, self.hook)
        self.hook.model_id = self.model_id

    def remove(self):
        remove_hook_from_module(self.model)
        self.hook.model_id = None

    def add_other_hook(self, hook: "UserCustomOffloadHook"):
        self.hook.add_other_hook(hook)


def custom_offload_with_hook(
    model_id: str,
    model: torch.nn.Module,
    execution_device: Union[str, int, torch.device] = None,
    offload_strategy: Optional["AutoOffloadStrategy"] = None,
):
    hook = CustomOffloadHook(execution_device=execution_device, offload_strategy=offload_strategy)
    user_hook = UserCustomOffloadHook(model_id=model_id, model=model, hook=hook)
    user_hook.attach()
    return user_hook


class AutoOffloadStrategy:
    """
    Offload strategy that should be used with `CustomOffloadHook` to automatically offload models to the CPU based on
    the available memory on the device.
    """

    def __init__(self, size_estimation_margin=0.1):
        self.size_estimation_margin = size_estimation_margin

    def __call__(self, hooks, model_id, model, execution_device):
        if len(hooks) == 0:
            return []

        current_module_size = get_memory_footprint(model)
        current_module_size *= 1 + self.size_estimation_margin

        mem_on_device = torch.cuda.mem_get_info(execution_device.index)[0]
        if current_module_size < mem_on_device:
            return []

        min_memory_offload = current_module_size - mem_on_device
        logger.info(f" search for models to offload in order to free up {min_memory_offload / 1024**3:.2f} GB memory")

        # exlucde models that's not currently loaded on the device
        module_sizes = dict(
            sorted(
                {hook.model_id: get_memory_footprint(hook.model) for hook in hooks}.items(),
                key=lambda x: x[1],
                reverse=True,
            )
        )

        def search_best_candidate(module_sizes, min_memory_offload):
            """
            search the optimal combination of models to offload to cpu, given a dictionary of module sizes and a
            minimum memory offload size. the combination of models should add up to the smallest modulesize that is
            larger than `min_memory_offload`
            """
            model_ids = list(module_sizes.keys())
            best_candidate = None
            best_size = float("inf")
            for r in range(1, len(model_ids) + 1):
                for candidate_model_ids in combinations(model_ids, r):
                    candidate_size = sum(
                        module_sizes[candidate_model_id] for candidate_model_id in candidate_model_ids
                    )
                    if candidate_size < min_memory_offload:
                        continue
                    else:
                        if best_candidate is None or candidate_size < best_size:
                            best_candidate = candidate_model_ids
                            best_size = candidate_size

            return best_candidate

        best_offload_model_ids = search_best_candidate(module_sizes, min_memory_offload)

        if best_offload_model_ids is None:
            # if no combination is found, meaning that we cannot meet the memory requirement, offload all models
            logger.warning("no combination of models to offload to cpu is found, offloading all models")
            hooks_to_offload = hooks
        else:
            hooks_to_offload = [hook for hook in hooks if hook.model_id in best_offload_model_ids]

        return hooks_to_offload


class ModelManager:
    def __init__(self):
        self.models = OrderedDict()
        self.model_hooks = None
        self._auto_offload_enabled = False

    def add(self, model_id, model):
        if model_id not in self.models:
            self.models[model_id] = model
            if self._auto_offload_enabled:
                self.enable_auto_cpu_offload(self._auto_offload_device)

    def remove(self, model_id):
        self.models.pop(model_id)
        if self._auto_offload_enabled:
            self.enable_auto_cpu_offload(self._auto_offload_device)

    def enable_auto_cpu_offload(self, device, size_estimation_margin=0.1):
        for model_id, model in self.models.items():
            if isinstance(model, torch.nn.Module) and hasattr(model, "_hf_hook"):
                remove_hook_from_module(model, recurse=True)

        self.disable_auto_cpu_offload()
        offload_strategy = AutoOffloadStrategy(size_estimation_margin=size_estimation_margin)
        device = torch.device(device)
        if device.index is None:
            device = torch.device(f"{device.type}:{0}")
        all_hooks = []
        for model_id, model in self.models.items():
            hook = custom_offload_with_hook(model_id, model, device, offload_strategy=offload_strategy)
            all_hooks.append(hook)

        for hook in all_hooks:
            other_hooks = [h for h in all_hooks if h is not hook]
            for other_hook in other_hooks:
                if other_hook.hook.execution_device == hook.hook.execution_device:
                    hook.add_other_hook(other_hook)

        self.model_hooks = all_hooks
        self._auto_offload_enabled = True
        self._auto_offload_device = device

    def disable_auto_cpu_offload(self):
        if self.model_hooks is None:
            self._auto_offload_enabled = False
            return

        for hook in self.model_hooks:
            hook.offload()
            hook.remove()
        if self.model_hooks:
            clear_device_cache()
        self.model_hooks = None
        self._auto_offload_enabled = False

    def __repr__(self):
        col_widths = {
            "id": max(15, max(len(id) for id in self.models.keys())),
            "class": max(25, max(len(model.__class__.__name__) for model in self.models.values())),
            "device": 10,
            "dtype": 15,
            "size": 10,
        }

        # Create the header
        sep_line = "=" * (sum(col_widths.values()) + len(col_widths) * 3 - 1) + "\n"
        dash_line = "-" * (sum(col_widths.values()) + len(col_widths) * 3 - 1) + "\n"

        output = "ModelManager:\n" + sep_line

        # Column headers
        output += f"{'Model ID':<{col_widths['id']}} | {'Class':<{col_widths['class']}} | "
        output += f"{'Device':<{col_widths['device']}} | {'Dtype':<{col_widths['dtype']}} | Size (GB) \n"
        output += dash_line

        # Model entries
        for model_id, model in self.models.items():
            device = model.device
            dtype = model.dtype
            size_bytes = get_memory_footprint(model)
            size_gb = size_bytes / (1024**3)

            output += f"{model_id:<{col_widths['id']}} | {model.__class__.__name__:<{col_widths['class']}} | "
            output += f"{str(device):<{col_widths['device']}} | {str(dtype):<{col_widths['dtype']}} | {size_gb:.2f}\n"

        output += sep_line
        return output
