# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team

import torch
import torch.utils._pytree as pytree

from torch._functorch.aot_autograd import create_aot_dispatcher_function
from torch._inductor.lowering import register_lowering, fallbacks, add_needs_realized_inputs
from torch._inductor.ir import TensorBox, FallbackKernel, Layout, IRNode
from torch._inductor.virtualized import V
from torch._inductor.scheduler import Scheduler

from .util import get_input_nodes
from .graph_param import DSGraphParamManager

original_create_aot_dispatcher_function = create_aot_dispatcher_function


def patch_create_aot_dispatcher_function(graph_id: int, z3_partition: bool, make_fw_graph, make_bw_graph, real_inputs,
                                         param_indices, param_manager):

    def wrapper_create_aot_dispatcher_function(
        flat_fn,
        fake_flat_args,
        aot_config,
        fake_mode,
        shape_env,
    ):

        def patch_compiler(original_compiler, bwd):

            def wrapped_compiler(gm, fake_inputs):
                dc_compiler = make_bw_graph if bwd else make_fw_graph
                dc_compiler(gm, fake_inputs)

                if z3_partition:
                    # Inductor validates input size estimated by the first trace, where ds tensor is materialized.
                    # We need to patch the input tensors to avoid the validation error.
                    patched_inputs = []
                    if bwd:
                        param_nodes_bw, _ = param_manager[graph_id].get_bwd_mapping(gm.graph)
                        param_names = [n.name for n in param_nodes_bw]
                    else:
                        param_names = param_manager[graph_id].param_names
                    input_nodes = get_input_nodes(gm.graph)

                    for in_node, in_v in zip(input_nodes, fake_inputs):
                        ds_param = in_node.name in param_names
                        if ds_param:
                            from torch._subclasses.fake_tensor import is_fake
                            from torch._dynamo.utils import to_fake_tensor
                            assert is_fake(in_v), f"Input {in_v} should be fake tensor"
                            patched_inputs.append(
                                to_fake_tensor(torch.empty([0], dtype=in_v.dtype, device=in_v.device), in_v.fake_mode))
                        else:
                            patched_inputs.append(in_v)

                    patched_inputs = tuple(patched_inputs)
                else:
                    patched_inputs = fake_inputs

                return original_compiler(gm, patched_inputs)

            return wrapped_compiler

        def wrap_partition_fn(partition_fn):

            def wrapped_partition_fn(*args, **kwargs):
                fw_module, bw_module = partition_fn(*args, **kwargs)

                # get parameter names
                pm = DSGraphParamManager(fw_module.graph, real_inputs, param_indices)

                def fix_placeholder_meta(graph):
                    for n in graph.nodes:
                        if n.op == "placeholder" and n.name in pm.param_names:
                            n.meta["val"] = torch.empty([0], dtype=n.meta["val"].dtype, device=n.meta["val"].device)

                fix_placeholder_meta(fw_module.graph)
                fix_placeholder_meta(bw_module.graph)

                return fw_module, bw_module

            return wrapped_partition_fn

        if z3_partition:
            aot_config.partition_fn = wrap_partition_fn(aot_config.partition_fn)

        aot_config.fw_compiler = patch_compiler(aot_config.fw_compiler, False)
        aot_config.bw_compiler = patch_compiler(aot_config.bw_compiler, True)
        aot_config.inference_compiler = aot_config.fw_compiler

        return original_create_aot_dispatcher_function(flat_fn, fake_flat_args, aot_config, fake_mode, shape_env)

    torch._functorch.aot_autograd.create_aot_dispatcher_function = wrapper_create_aot_dispatcher_function


def register_custom_ops():

    def fallback_handler_no_reuse(kernel, never_reuse_input, force_free_input, add_to_fallback_set=True):
        if add_to_fallback_set:
            fallbacks.add(kernel)

        def handler(*args, **kwargs):

            def wrap_tensors(x):
                out = TensorBox.create(x) if isinstance(x, torch._inductor.ir.IRNode) else x
                if out is not None:
                    V.graph.never_reuse_buffers.add(out.get_name())
                return out

            class CustomDCKernel(FallbackKernel):

                def __init__(self, op, *args, **kwargs):
                    super().__init__(op, *args, **kwargs)

                    def add_to_never_reuse(x):
                        if isinstance(x, IRNode):
                            assert hasattr(x, "get_name"), f"x doesn't have get_name {x.__class__}"
                            V.graph.never_reuse_buffers.add(x.get_name())

                    if never_reuse_input:
                        pytree.tree_map(add_to_never_reuse, args)

                def get_var_name_for_arg(self, arg: str):
                    if arg.isidentifier():
                        return arg

                    import re
                    match = re.match(r"reinterpret_tensor\((\w+),", arg)
                    if match:
                        return match.group(1)
                    return None

                def codegen(self, wrapper):
                    if not force_free_input:
                        return super().codegen(wrapper)

                    kernel = self.op_overload
                    self.codegen_comment(wrapper)
                    args = [*self.codegen_args(), *self.codegen_kwargs()]

                    V.graph.wrapper_code.generate_fallback_kernel(self, args)
                    if isinstance(self.layout, Layout):
                        self.codegen_size_asserts(wrapper)

                    var_name = self.get_var_name_for_arg(args[0])
                    if var_name:
                        wrapper.writeline(f"{var_name} = None")

                    self.codegen_unbacked_symbol_defs(wrapper)

            kernel_cls = CustomDCKernel if force_free_input else FallbackKernel
            return pytree.tree_map(wrap_tensors, kernel_cls.create(kernel, *args, **kwargs))

        return handler

    def register_fallback_no_reuse(op_overload, never_reuse_input=False, force_free_input=False):
        add_needs_realized_inputs(op_overload)
        return register_lowering(op_overload, type_promotion_kind=None)(fallback_handler_no_reuse(
            op_overload, never_reuse_input=never_reuse_input, force_free_input=force_free_input))

    # Inductor tries to reuse output buffer when possible. We need to disable this behavior for some custom ops.
    # -> It seems that memory region is still reused in some cases. So we clone the inputs for some ops.
    register_fallback_no_reuse(torch.ops.dc.allgather_param.default, never_reuse_input=False)
    register_fallback_no_reuse(torch.ops.dc.wait_allgather.default, never_reuse_input=True)
    register_fallback_no_reuse(torch.ops.dc.release_param.default, never_reuse_input=True)
    register_fallback_no_reuse(torch.ops.dc.reduce_grad.default, never_reuse_input=True, force_free_input=True)
    register_fallback_no_reuse(torch.ops.dc.free_tensors.default, never_reuse_input=True)

    if not hasattr(Scheduler, "is_dc_patched") or not Scheduler.is_dc_patched:
        Scheduler.is_dc_patched = True
        Scheduler.dead_node_elimination = lambda _: None
