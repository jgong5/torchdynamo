import dataclasses
import functools
import logging
from typing import List

import functorch
import torch.fx
from functorch.compile import make_boxed_compiler
from functorch.compile import min_cut_rematerialization_partition
from torch._subclasses.fake_tensor import FakeTensor
from torch.utils._mode_utils import no_dispatch

from torchdynamo.optimizations.backends import aot_autograd
from torchdynamo.optimizations.normalize import normalize_ir
from torchdynamo.utils import dynamo_timed
from torchdynamo.utils import preserve_rng_state

from . import config
from . import overrides
from .debug import DebugContext
from .decomposition import select_decomp_table
from .graph import GraphLowering
from .utils import has_incompatible_cudagraph_ops
from .virtualized import V

log = logging.getLogger(__name__)
ALIGNMENT = 16


@dataclasses.dataclass
class BoxedBool:
    value: bool

    def __bool__(self):
        return self.value

    @staticmethod
    def disable(obj):
        if isinstance(obj, BoxedBool):
            obj.value = False
            return obj
        return False


# copy_ fails when trying to write to tensors with memory overlap,
# for expanded dimensions (a dimension which used to have size 1 -> ?)
# we can select one element from that dimension and write to it
# to achieve writing to all values of that dimension of the input tensor
def get_expanded_dims(t):
    return [i for i in range(t.ndim) if t.stride(i) == 0 and t.size(i) != 1]


def index_expanded_dims(t, expanded_dims):
    for expanded_dim in expanded_dims:
        t = torch.ops.aten.slice(t, expanded_dim, 0, 1)
    return t


def complex_memory_overlap(t):
    indexed_tensor = index_expanded_dims(t, get_expanded_dims(t))
    return torch._debug_has_internal_overlap(indexed_tensor) != 0


@DebugContext.wrap
@no_dispatch()
def compile_fx_inner(
    gm: torch.fx.GraphModule,
    example_inputs: List[torch.Tensor],
    cudagraphs=None,
    num_fixed=0,
    is_backward=False,
):
    log.info("Compiling %s graph", "BACKWARDS" if is_backward else "FORWARDS")
    V.debug.fx_graph(gm, example_inputs)

    if cudagraphs is None:
        cudagraphs = config.triton.cudagraphs

    graph = GraphLowering(gm, num_dynamic_inputs=len(example_inputs))
    with V.set_graph_handler(graph):
        graph.run(*example_inputs)
        compiled_fn = graph.compile_to_fn()

    complex_memory_overlap_inputs = any(
        complex_memory_overlap(t) for t in example_inputs
    )

    if (
        cudagraphs
        and set(graph.device_types) == {"cuda"}
        and not graph.mutated_inputs
        and not has_incompatible_cudagraph_ops(gm)
        and not complex_memory_overlap_inputs
    ):
        compiled_fn = cudagraphify(
            compiled_fn, example_inputs, static_input_idxs=range(num_fixed)
        )
    elif cudagraphs:
        BoxedBool.disable(cudagraphs)

        if len(set(graph.device_types)) > 1:
            log.warning("skipping cudagraphs due to multiple devices")
        elif set(graph.device_types) == {"cuda"}:
            if graph.mutated_inputs:
                log.warning("skipping cudagraphs due to input mutation")
            elif complex_memory_overlap_inputs:
                log.warning("skipping cudagraphs due to complex input striding")

    return align_inputs(compiled_fn, example_inputs, range(num_fixed))


def clone_preserve_strides(x):
    needed_size = (
        sum((shape - 1) * stride for shape, stride in zip(x.size(), x.stride())) + 1
    )
    buffer = torch.as_strided(x, (needed_size,), (1,)).clone()
    return torch.as_strided(buffer, x.size(), x.stride())


def align_inputs(model, inputs, static_input_idxs=()):
    check_inputs = [
        i
        for i in range(len(inputs))
        if (i not in static_input_idxs or (inputs[i].data_ptr() % ALIGNMENT) != 0)
        and inputs[i].device.type == "cuda"
    ]

    if len(check_inputs) == 0:
        return model

    def run(*new_inputs):
        for i in check_inputs:
            if new_inputs[i].data_ptr() % ALIGNMENT:
                if isinstance(new_inputs, tuple):
                    new_inputs = list(new_inputs)
                new_inputs[i] = clone_preserve_strides(new_inputs[i])
        return model(*new_inputs)

    return run


@dynamo_timed
def cudagraphify(model, inputs, static_input_idxs=()):
    # if using fake tensors, defer cudagraphs until we get real inputs at runtime
    if not any(isinstance(inp, FakeTensor) for inp in inputs):
        return cudagraphify_impl(model, inputs, static_input_idxs)

    compiled_fn = None

    def run(*new_inputs):
        nonlocal compiled_fn
        if compiled_fn is None:
            with preserve_rng_state():
                compiled_fn = cudagraphify_impl(model, new_inputs, static_input_idxs)

        return compiled_fn(*new_inputs)

    return run


def remove_unaligned_input_idxs(inputs, static_input_idxs):
    """
    We require all inputs to be aligned, so introduce a copy for any
    that aren't.
    """
    aligned_static_input_idxs = {
        idx for idx in static_input_idxs if (inputs[idx].data_ptr() % ALIGNMENT) == 0
    }
    if len(aligned_static_input_idxs) != len(static_input_idxs):
        return aligned_static_input_idxs
    return static_input_idxs


def cudagraphify_impl(model, inputs, static_input_idxs=()):
    """
    Assumes inputs[static_input_idxs[i]] are always the same memory address
    """
    static_input_idxs = remove_unaligned_input_idxs(inputs, static_input_idxs)

    def static_input(x):
        """
        Copy and input while preserving strides
        """
        # TODO(jansel): figure out why this version doesn't work:
        # return torch.empty_strided(x.size(), x.stride(), dtype=x.dtype, device=x.device)
        needed_size = (
            sum((shape - 1) * stride for shape, stride in zip(x.size(), x.stride())) + 1
        )
        buffer = torch.zeros(needed_size, dtype=x.dtype, device=x.device)
        return torch.as_strided(buffer, x.size(), x.stride())

    assert isinstance(inputs, (list, tuple))
    static_inputs = [
        static_input(x) if idx not in static_input_idxs else x
        for idx, x in enumerate(inputs)
    ]

    inps_expanded_dims = [
        get_expanded_dims(x) if idx not in static_input_idxs else []
        for idx, x in enumerate(inputs)
    ]

    # warmup
    torch.cuda.synchronize()
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        model(*static_inputs)
    stream.synchronize()
    torch.cuda.current_stream().wait_stream(stream)
    torch.cuda.synchronize()

    # record
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph, stream=stream):
        static_outputs = model(*static_inputs)
    if not isinstance(static_outputs, (list, tuple)):
        static_outputs = (static_outputs,)

    if config.size_asserts:

        def run(*new_inputs):
            assert len(static_inputs) == len(new_inputs)
            for idx, (dst, src, expanded_dims) in enumerate(
                zip(static_inputs, new_inputs, inps_expanded_dims)
            ):
                if idx in static_input_idxs:
                    assert dst.data_ptr() == src.data_ptr()
                else:
                    # TODO - could make one single op of multiple slices
                    # and avoid dispatch.
                    # Could also pre-index the `dst` tensors
                    dst = index_expanded_dims(dst, expanded_dims)
                    src = index_expanded_dims(src, expanded_dims)
                    dst.copy_(src)
            graph.replay()
            return static_outputs

    else:
        copy_indices = [
            idx for idx in range(len(static_inputs)) if idx not in static_input_idxs
        ]

        def run(*new_inputs):
            for idx in copy_indices:
                src = index_expanded_dims(static_inputs[idx], inps_expanded_dims[idx])
                dst = index_expanded_dims(new_inputs[idx], inps_expanded_dims[idx])
                dst.copy_(src)
            graph.replay()
            return static_outputs

    return run


def count_tangents(fx_g: torch.fx.GraphModule):
    """
    Infers which inputs are static for a backwards graph
    """

    def is_not_gradout(x):
        return "tangents" not in x.name

    arg_count = 0
    static_arg_idxs = []
    for n in fx_g.graph.nodes:
        if n.op == "placeholder":
            if is_not_gradout(n):
                static_arg_idxs.append(arg_count)
            arg_count += 1

    assert static_arg_idxs == list(range(len(static_arg_idxs)))
    return len(static_arg_idxs)


def compile_fx(model_: torch.fx.GraphModule, example_inputs_: List[torch.Tensor]):
    """Main entrypoint to a compile given FX graph"""
    functorch.compile.config.use_functionalize = True
    functorch.compile.config.use_fake_tensor = True

    with overrides.patch_functions():
        model_ = normalize_ir(model_, example_inputs_)
        model_ = overrides.replace_fx(model_)
    num_example_inputs = len(example_inputs_)
    cudagraphs = BoxedBool(config.triton.cudagraphs)

    @dynamo_timed
    def fw_compiler(model: torch.fx.GraphModule, example_inputs):
        fixed = len(example_inputs) - num_example_inputs
        return compile_fx_inner(
            model, example_inputs, num_fixed=fixed, cudagraphs=cudagraphs
        )

    @dynamo_timed
    def bw_compiler(model: torch.fx.GraphModule, example_inputs):
        fixed = count_tangents(model)
        return compile_fx_inner(
            model,
            example_inputs,
            num_fixed=fixed,
            cudagraphs=cudagraphs,
            is_backward=True,
        )

    with overrides.patch_functions():
        return aot_autograd(
            model_,
            example_inputs_,
            fw_compiler=make_boxed_compiler(fw_compiler),
            bw_compiler=make_boxed_compiler(bw_compiler),
            decompositions=select_decomp_table(),
            partition_fn=functools.partial(
                min_cut_rematerialization_partition, compiler="inductor"
            ),
        )
