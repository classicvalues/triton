# flake8: noqa: F821,F841
import copy
import itertools
import re
from typing import Optional, Union

import numpy as np
import pytest
import torch
from numpy.random import RandomState

import triton
import triton._C.libtriton.triton as _triton
import triton.language as tl
from triton.code_gen import TensorWrapper, reinterpret

int_dtypes = ['int8', 'int16', 'int32', 'int64']
uint_dtypes = ['uint8', 'uint16', 'uint32', 'uint64']
float_dtypes = ['float16', 'float32', 'float64']
dtypes = int_dtypes + uint_dtypes + float_dtypes


def _bitwidth(dtype: str) -> int:
    # ex.: "int64" -> 64
    return int(re.search(r'(\d+)$', dtype).group(1))


def numpy_random(shape, dtype_str, rs: Optional[RandomState] = None, low=None, high=None):
    """
    Override `rs` if you're calling this function twice and don't want the same
    result for both calls.
    """
    if isinstance(shape, int):
        shape = (shape, )
    if rs is None:
        rs = RandomState(seed=17)
    dtype = getattr(np, dtype_str)
    if dtype_str in int_dtypes + uint_dtypes:
        iinfo = np.iinfo(getattr(np, dtype_str))
        low = iinfo.min if low is None else max(low, iinfo.min)
        high = iinfo.max if high is None else min(high, iinfo.max)
        x = rs.randint(low, high, shape, dtype=dtype)
        x[x == 0] = 1  # Hack. Never return zero so tests of division don't error out.
        return x
    elif dtype_str in float_dtypes:
        return rs.normal(0, 1, shape).astype(dtype)
    else:
        raise RuntimeError(f'Unknown dtype {dtype_str}')


def to_triton(x: np.ndarray, device='cuda') -> Union[TensorWrapper, torch.Tensor]:
    t = x.dtype.name
    if t in uint_dtypes:
        signed_type_name = t.lstrip('u')  # e.g. "uint16" -> "int16"
        x_signed = x.astype(getattr(np, signed_type_name))
        return reinterpret(torch.tensor(x_signed, device=device), getattr(tl, t))
    else:
        return torch.tensor(x, device=device)


def torch_dtype_name(dtype) -> str:
    if isinstance(dtype, triton.language.dtype):
        return dtype.name
    elif isinstance(dtype, torch.dtype):
        # 'torch.int64' -> 'int64'
        m = re.match(r'^torch\.(\w+)$', str(dtype))
        return m.group(1)
    else:
        raise TypeError(f'not a triton or torch dtype: {type(dtype)}')


def to_numpy(x):
    if isinstance(x, TensorWrapper):
        return x.base.cpu().numpy().astype(getattr(np, torch_dtype_name(x.dtype)))
    elif isinstance(x, torch.Tensor):
        return x.cpu().numpy()
    else:
        raise ValueError(f"Not a triton-compatible tensor: {x}")


def patch_kernel(template, to_replace):
    kernel = triton.JITFunction(template.fn)
    for key, value in to_replace.items():
        kernel.src = kernel.src.replace(key, value)
    return kernel


@pytest.mark.parametrize("dtype_x", [dtype_x for dtype_x in dtypes])
def test_empty_kernel(dtype_x, device='cuda'):
    SIZE = 128

    @triton.jit
    def kernel(X, SIZE: tl.constexpr):
        pass
    x = to_triton(numpy_random(SIZE, dtype_str=dtype_x), device=device)
    kernel[(1, )](x, SIZE=SIZE, num_warps=4)


# generic test functions
def _test_unary(dtype_x, expr, numpy_expr=None, device='cuda'):
    SIZE = 128
    # define the kernel / launch-grid

    @triton.jit
    def kernel(Z, X, SIZE: tl.constexpr):
        off = tl.arange(0, SIZE)
        x = tl.load(X + off)
        z = GENERATE_TEST_HERE
        tl.store(Z + off, z)

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': expr})
    # inputs
    x = numpy_random(SIZE, dtype_str=dtype_x)
    if 'log' in expr:
        x = np.abs(x) + 0.01
    # reference result
    z_ref = eval(expr if numpy_expr is None else numpy_expr)
    # triton result
    x_tri = to_triton(x, device=device)
    z_tri = to_triton(np.empty_like(z_ref), device=device)
    kernel[(1, )](z_tri, x_tri, SIZE=SIZE, num_warps=4)
    # compare
    np.testing.assert_allclose(z_ref, to_numpy(z_tri), rtol=0.01)


def _binary_op_dtype_override(a: str, b: str) -> Optional[np.dtype]:
    """
    Given two dtype strings, returns the numpy dtype Triton thinks binary
    operations on the two types should return. Returns None if the return value
    matches numpy. This is generally needed because Triton and pytorch return
    narrower floating point types than numpy in mixed operations, and because
    Triton follows C/C++ semantics around mixed signed/unsigned operations, and
    numpy/pytorch do not.
    """
    overrides = {
        ('float16', 'int16'): np.float16,
        ('float16', 'int32'): np.float16,
        ('float16', 'int64'): np.float16,
        ('float16', 'uint16'): np.float16,
        ('float16', 'uint32'): np.float16,
        ('float16', 'uint64'): np.float16,
        ('int8', 'uint8'): np.uint8,
        ('int8', 'uint16'): np.uint16,
        ('int8', 'uint32'): np.uint32,
        ('int8', 'uint64'): np.uint64,
        ('int16', 'uint16'): np.uint16,
        ('int16', 'uint32'): np.uint32,
        ('int16', 'uint64'): np.uint64,
        ('int32', 'uint32'): np.uint32,
        ('int32', 'uint64'): np.uint64,
        ('int64', 'uint64'): np.uint64,
    }
    key = (a, b) if a < b else (b, a)
    return overrides.get(key)


def _test_binary(dtype_x, dtype_y, expr, numpy_expr=None, mode_x='real', mode_y='real', device='cuda', y_low=None, y_high=None):
    SIZE = 128
    # define the kernel / launch-grid

    @triton.jit
    def kernel(Z, X, Y, SIZE: tl.constexpr):
        off = tl.arange(0, SIZE)
        x = tl.load(X + off)
        y = tl.load(Y + off)
        z = GENERATE_TEST_HERE
        tl.store(Z + off, z)

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': expr})
    # inputs
    rs = RandomState(17)
    x = numpy_random(SIZE, dtype_str=dtype_x, rs=rs)
    y = numpy_random(SIZE, dtype_str=dtype_y, rs=rs, low=y_low, high=y_high)
    if mode_x == 'nan':
        x[:] = float('nan')
    if mode_y == 'nan':
        y[:] = float('nan')
    # reference result
    z_ref = eval(expr if numpy_expr is None else numpy_expr)
    dtype_z = _binary_op_dtype_override(dtype_x, dtype_y)
    if dtype_z is not None:
        z_ref = z_ref.astype(dtype_z)
    # triton result
    x_tri = to_triton(x, device=device)
    y_tri = to_triton(y, device=device)
    z_tri = to_triton(np.empty(SIZE, dtype=z_ref.dtype), device=device)
    kernel[(1, )](z_tri, x_tri, y_tri, SIZE=SIZE, num_warps=4)
    np.testing.assert_allclose(z_ref, to_numpy(z_tri), err_msg=expr, rtol=0.01)


def _mod_operation_ill_conditioned(dtype_x, dtype_y) -> bool:
    # The result of x % y is ill-conditioned if x % y is much smaller than x.
    # pytorch/CUDA has slightly different (probably better) rounding on
    # remainders than stock LLVM. We currently don't expect to match it
    # bit-for-bit.
    return (dtype_x, dtype_y) in [
        ('int32', 'float16'),
        ('int32', 'float32'),
        ('int64', 'float16'),
        ('int64', 'float32'),
        ('int64', 'float64'),
        ('uint16', 'float16'),
        ('uint16', 'float32'),
        ('uint32', 'float16'),
        ('uint32', 'float32'),
        ('uint64', 'float16'),
        ('uint64', 'float32'),
        ('uint64', 'float64'),
    ]

# ---------------
# test binary ops
# ---------------


@pytest.mark.parametrize("dtype_x, dtype_y, op", [
    (dtype_x, dtype_y, op)
    for op in ['+', '-', '*', '/', '%']
    for dtype_x in dtypes
    for dtype_y in dtypes
])
def test_bin_op(dtype_x, dtype_y, op, device='cuda'):
    expr = f' x {op} y'
    if op == '%' and dtype_x in int_dtypes + uint_dtypes and dtype_y in int_dtypes + uint_dtypes:
        # LLVM has 'numpy.fmod', not 'numpy.remainder', semantics on integer remainders.
        numpy_expr = 'np.fmod(x, y)'
    elif op in ('/', '%') and dtype_x in ('int16', 'float16') and dtype_y in ('int16', 'float16'):
        # Triton promotes 16-bit floating-point / and % to 32-bit because there
        # are no native div or FRem operations on float16. Since we have to
        # convert anyway, we may as well take the accuracy bump.
        numpy_expr = f'x.astype(np.float32) {op} y.astype(np.float32)'
    elif (dtype_x in uint_dtypes and dtype_y in int_dtypes and _bitwidth(dtype_x) >= _bitwidth(dtype_y)):
        numpy_expr = f'x.astype(np.{dtype_x}) {op} y.astype(np.{dtype_x})'
    elif (dtype_y in uint_dtypes and dtype_x in int_dtypes and _bitwidth(dtype_y) >= _bitwidth(dtype_x)):
        numpy_expr = f'x.astype(np.{dtype_y}) {op} y.astype(np.{dtype_y})'
    else:
        numpy_expr = None
    if op == '%' and _mod_operation_ill_conditioned(dtype_x, dtype_y):
        with pytest.raises(AssertionError, match='Not equal to tolerance'):
            _test_binary(dtype_x, dtype_y, expr, numpy_expr, device=device)
    elif (op in ('%', '/') and
          ((dtype_x in int_dtypes and dtype_y in uint_dtypes) or
           (dtype_x in uint_dtypes and dtype_y in int_dtypes))):
        with pytest.raises(triton.code_gen.CompilationError) as exc_info:
            _test_binary(dtype_x, dtype_y, expr, numpy_expr, device=device)
        assert re.match('Cannot use .* because they have different signedness', str(exc_info.value.__cause__))
    else:
        _test_binary(dtype_x, dtype_y, expr, numpy_expr, device=device)


@pytest.mark.parametrize("dtype_x, dtype_y",
                         [(dtype_x, dtype_y) for dtype_x in int_dtypes for dtype_y in int_dtypes] +
                         [(dtype_x, dtype_y) for dtype_x in uint_dtypes for dtype_y in uint_dtypes]
                         )
def test_floordiv(dtype_x, dtype_y, device='cuda'):
    # Triton has IEEE, not numpy/torch, semantics for %, and those carry
    # through to //, so we have to use a nonstandard expression to get a
    # reference result for //.
    expr = 'x // y'
    numpy_expr = '((x - np.fmod(x, y)) / y)'
    _test_binary(dtype_x, dtype_y, expr, numpy_expr, device=device)


# ---------------
# test bitwise ops
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_y, op", [
    (dtype_x, dtype_y, op)
    for op in ['&', '|', '^']
    for dtype_x in dtypes
    for dtype_y in dtypes
])
def test_bitwise_op(dtype_x, dtype_y, op, device='cuda'):
    expr = f'x {op} y'
    if (dtype_x in uint_dtypes and dtype_y in int_dtypes and _bitwidth(dtype_x) >= _bitwidth(dtype_y)):
        numpy_expr = f'x.astype(np.{dtype_x}) {op} y.astype(np.{dtype_x})'
    elif (dtype_y in uint_dtypes and dtype_x in int_dtypes and _bitwidth(dtype_y) >= _bitwidth(dtype_x)):
        numpy_expr = f'x.astype(np.{dtype_y}) {op} y.astype(np.{dtype_y})'
    else:
        numpy_expr = None
    if 'float' in dtype_x + dtype_y:
        with pytest.raises(triton.code_gen.CompilationError) as exc_info:
            _test_binary(dtype_x, dtype_y, expr, numpy_expr='np.array([])', device=device)
        # The CompilationError must have been caused by a C++ exception with this text.
        assert re.match('invalid operands of type', str(exc_info.value.__cause__))
    else:
        _test_binary(dtype_x, dtype_y, expr, numpy_expr, device=device)


@pytest.mark.parametrize("dtype_x, dtype_y, op", [
    (dtype_x, dtype_y, op)
    for op in ['<<', '>>']
    for dtype_x in int_dtypes + uint_dtypes
    for dtype_y in int_dtypes + uint_dtypes
])
def test_shift_op(dtype_x, dtype_y, op, device='cuda'):
    expr = f'x {op} y'
    bw = max(_bitwidth(dtype_x), _bitwidth(dtype_y))
    dtype_z = f'uint{bw}'
    numpy_expr = f'x.astype(np.{dtype_z}) {op} y.astype(np.{dtype_z})'
    _test_binary(dtype_x, dtype_y, expr, numpy_expr, device=device, y_low=0, y_high=65)


# ---------------
# test compare ops
# ---------------
ops = ['==', '!=', '>', '<', '>=', '<=']


@pytest.mark.parametrize("dtype_x, dtype_y, op, mode_x, mode_y",
                         # real
                         [
                             (dtype_x, dtype_y, op, 'real', 'real')
                             for op in ops
                             for dtype_x in dtypes
                             for dtype_y in dtypes
                         ] +
                         # NaNs
                         [('float32', 'float32', op, mode_x, mode_y)
                             for op in ops
                             for mode_x, mode_y in [('nan', 'real'),
                                                    ('real', 'nan'),
                                                    ('nan', 'nan')]

                          ])
def test_compare_op(dtype_x, dtype_y, op, mode_x, mode_y, device='cuda'):
    expr = f'x {op} y'
    if (dtype_x in uint_dtypes and dtype_y in int_dtypes and _bitwidth(dtype_x) >= _bitwidth(dtype_y)):
        numpy_expr = f'x.astype(np.{dtype_x}) {op} y.astype(np.{dtype_x})'
    elif (dtype_y in uint_dtypes and dtype_x in int_dtypes and _bitwidth(dtype_y) >= _bitwidth(dtype_x)):
        numpy_expr = f'x.astype(np.{dtype_y}) {op} y.astype(np.{dtype_y})'
    else:
        numpy_expr = None
    _test_binary(dtype_x, dtype_y, expr, numpy_expr, mode_x=mode_x, mode_y=mode_y, device=device)


# ---------------
# test unary ops
# ---------------
@pytest.mark.parametrize("dtype_x, expr", [
    (dtype_x, ' -x') for dtype_x in dtypes
] + [
    (dtype_x, ' ~x') for dtype_x in int_dtypes
])
def test_unary_op(dtype_x, expr, device='cuda'):
    _test_unary(dtype_x, expr, device=device)

# ----------------
# test math ops
# ----------------
# @pytest.mark.paramterize("expr", [
#     'exp', 'log', 'cos', 'sin'
# ])


@pytest.mark.parametrize("expr", [
    'exp', 'log', 'cos', 'sin'
])
def test_math_op(expr, device='cuda'):
    _test_unary('float32', f'tl.{expr}(x)', f'np.{expr}(x) ', device=device)


# ----------------
# test indexing
# ----------------


def make_ptr_str(name, shape):
    rank = len(shape)
    offsets = []
    stride = 1
    for i in reversed(range(rank)):
        idx = ', '.join([':' if ii == i else 'None' for ii in range(rank)])
        offsets += [f'tl.arange(0, {shape[i]})[{idx}]*{stride}']
        stride *= shape[i]
    return f"{name} + {' + '.join(offsets)}"


@pytest.mark.parametrize("expr, dtype_str", [
    (f'x[{s}]', d)
    for s in ['None, :', ':, None', 'None, :, :', ':, :, None']
    for d in ['int32', 'uint32', 'uint16']
])
def test_index1d(expr, dtype_str, device='cuda'):
    rank_x = expr.count(':')
    rank_y = expr.count(',') + 1
    shape_x = [32 for _ in range(rank_x)]
    shape_z = [32 for _ in range(rank_y)]

    # Triton kernel
    @triton.jit
    def kernel(Z, X, SIZE: tl.constexpr):
        m = tl.arange(0, SIZE)
        n = tl.arange(0, SIZE)
        x = tl.load(X_PTR_EXPR)
        z = GENERATE_TEST_HERE
        tl.store(Z_PTR_EXPR, z)

    to_replace = {
        'X_PTR_EXPR': make_ptr_str('X', shape_x),
        'Z_PTR_EXPR': make_ptr_str('Z', shape_z),
        'GENERATE_TEST_HERE': expr,
    }
    kernel = patch_kernel(kernel, to_replace)

    # torch result
    x = numpy_random(shape_x, dtype_str=dtype_str)
    y = np.zeros(shape_z, dtype=getattr(np, dtype_str))
    z_ref = eval(expr) + y
    # triton result
    z_tri = to_triton(np.empty_like(z_ref), device=device)
    x_tri = to_triton(x)
    kernel[(1, )](z_tri, x_tri, num_warps=1, SIZE=shape_x[0])
    # compare
    assert (z_ref == to_numpy(z_tri)).all()


# ---------------
# test tuples
# ---------------


@triton.jit
def fn(a, b):
    return a + b, \
        a - b, \
        a * b


def test_tuples():
    device = 'cuda'

    @triton.jit
    def with_fn(X, Y, A, B, C):
        x = tl.load(X)
        y = tl.load(Y)
        a, b, c = fn(x, y)
        tl.store(A, a)
        tl.store(B, b)
        tl.store(C, c)

    @triton.jit
    def without_fn(X, Y, A, B, C):
        x = tl.load(X)
        y = tl.load(Y)
        a, b, c = x + y, x - y, x * y
        tl.store(A, a)
        tl.store(B, b)
        tl.store(C, c)

    x = torch.tensor([1.3], device=device, dtype=torch.float32)
    y = torch.tensor([1.9], device=device, dtype=torch.float32)
    a_tri = torch.tensor([0], device=device, dtype=torch.float32)
    b_tri = torch.tensor([0], device=device, dtype=torch.float32)
    c_tri = torch.tensor([0], device=device, dtype=torch.float32)
    for kernel in [with_fn, without_fn]:
        kernel[(1, )](x, y, a_tri, b_tri, c_tri, num_warps=1)
        a_ref, b_ref, c_ref = x + y, x - y, x * y
        assert a_tri == a_ref
        assert b_tri == b_ref
        assert c_tri == c_ref


# ---------------
# test atomics
# ---------------
@pytest.mark.parametrize("op, dtype_x_str, mode", itertools.chain.from_iterable([
    [
        ('add', 'float16', mode),
        ('add', 'uint32', mode), ('add', 'int32', mode), ('add', 'float32', mode),
        ('max', 'uint32', mode), ('max', 'int32', mode), ('max', 'float32', mode),
        ('min', 'uint32', mode), ('min', 'int32', mode), ('min', 'float32', mode),
    ]
    for mode in ['all_neg', 'all_pos', 'min_neg', 'max_pos']]))
def test_atomic_rmw(op, dtype_x_str, mode, device='cuda'):
    n_programs = 5

    # triton kernel
    @triton.jit
    def kernel(X, Z):
        pid = tl.program_id(0)
        x = tl.load(X + pid)
        old = GENERATE_TEST_HERE

    kernel = patch_kernel(kernel, {'GENERATE_TEST_HERE': f'tl.atomic_{op}(Z, x)'})
    numpy_op = {'add': np.sum, 'max': np.max, 'min': np.min}[op]
    max_neutral = float('-inf') if dtype_x_str in float_dtypes else np.iinfo(getattr(np, dtype_x_str)).min
    min_neutral = float('inf') if dtype_x_str in float_dtypes else np.iinfo(getattr(np, dtype_x_str)).max
    neutral = {'add': 0, 'max': max_neutral, 'min': min_neutral}[op]

    # triton result
    rs = RandomState(17)
    x = numpy_random((n_programs, ), dtype_str=dtype_x_str, rs=rs)
    if mode == 'all_neg':
        x = -np.abs(x)
    if mode == 'all_pos':
        x = np.abs(x)
    if mode == 'min_neg':
        idx = rs.randint(n_programs, size=(1, )).item()
        x[idx] = -np.max(np.abs(x)) - 1
    if mode == 'max_pos':
        idx = rs.randint(n_programs, size=(1, )).item()
        x[idx] = np.max(np.abs(x)) + 1
    x_tri = to_triton(x, device=device)

    z_tri = to_triton(np.array([neutral], dtype=getattr(np, dtype_x_str)), device=device)
    kernel[(n_programs, )](x_tri, z_tri)
    # torch result
    z_ref = numpy_op(x).astype(getattr(np, dtype_x_str))
    # compare
    exact = op not in ['add']
    if exact:
        assert z_ref.item() == to_numpy(z_tri).item()
    else:
        np.testing.assert_allclose(z_ref, to_numpy(z_tri), rtol=0.01)


# ---------------
# test cast
# ---------------
@pytest.mark.parametrize("dtype_x, dtype_z, bitcast", [
    (dtype_x, dtype_z, False)
    for dtype_x in dtypes
    for dtype_z in dtypes
] + [
    ('float32', 'bfloat16', False),
    ('bfloat16', 'float32', False),
    ('float32', 'int32', True),
] + [
    (f'uint{x}', f'int{x}', True) for x in [8, 16, 32, 64]
] + [
    (f'int{x}', f'uint{x}', True) for x in [8, 16, 32, 64]
])
def test_cast(dtype_x, dtype_z, bitcast, device='cuda'):
    # This is tricky because numpy doesn't have bfloat, and torch doesn't have uints.
    x0 = 43 if dtype_x in int_dtypes else 43.5
    if dtype_x.startswith('bfloat'):
        x_tri = torch.tensor([x0], dtype=getattr(torch, dtype_x), device=device)
    else:
        x = np.array([x0], dtype=getattr(np, dtype_x))
        x_tri = to_triton(x)

    # triton kernel
    @triton.jit
    def kernel(X, Z, BITCAST: tl.constexpr):
        x = tl.load(X)
        z = x.to(Z.dtype.element_ty, bitcast=BITCAST)
        tl.store(Z, z)

    # triton result
    if dtype_z.startswith('bfloat'):
        z_tri = torch.empty((1,), dtype=getattr(torch, dtype_z), device=device)
    else:
        z_tri = to_triton(np.empty((1, ), dtype=getattr(np, dtype_z)), device=device)
    kernel[(1, )](x_tri, z_tri, BITCAST=bitcast)
    # torch result
    if dtype_z.startswith('bfloat') or dtype_x.startswith('bfloat'):
        assert bitcast is False
        z_ref = x_tri.to(z_tri.dtype)
        assert z_tri == z_ref
    else:
        if bitcast:
            z_ref = x.view(getattr(np, dtype_z))
        else:
            z_ref = x.astype(getattr(np, dtype_z))
        assert to_numpy(z_tri) == z_ref

# ---------------
# test reduce
# ---------------


@pytest.mark.parametrize("dtype_str, shape",
                         [(dtype, shape)
                          for dtype in dtypes
                          for shape in [128, 512]])
def test_reduce1d(dtype_str, shape, device='cuda'):

    # triton kernel
    @triton.jit
    def kernel(X, Z, BLOCK: tl.constexpr):
        x = tl.load(X + tl.arange(0, BLOCK))
        tl.store(Z, tl.sum(x, axis=0))

    rs = RandomState(17)
    x = numpy_random((shape,), dtype_str=dtype_str, rs=rs)
    # numpy result
    z_ref = np.sum(x).astype(getattr(np, dtype_str))
    # triton result
    x_tri = to_triton(x, device=device)
    z_tri = to_triton(numpy_random((1,), dtype_str=dtype_str, rs=rs), device=device)
    kernel[(1,)](x_tri, z_tri, BLOCK=shape)
    # compare
    np.testing.assert_allclose(z_ref, to_numpy(z_tri), rtol=0.01)


@pytest.mark.parametrize("dtype_str, shape, axis", [
    (dtype, (1, 1024), 1) for dtype in ['float32', 'uint32']
])
def test_reduce2d(dtype_str, shape, axis, device='cuda'):
    # triton kernel
    @triton.jit
    def kernel(X, Z, BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr, AXIS: tl.constexpr):
        range_m = tl.arange(0, BLOCK_M)
        range_n = tl.arange(0, BLOCK_N)
        x = tl.load(X + range_m[:, None] * BLOCK_N + range_n[None, :])
        z = tl.sum(x, axis=AXIS)
        tl.store(Z + range_m, z)
    # input
    x = numpy_random(shape, dtype_str=dtype_str)
    # triton result
    x_tri = to_triton(x)
    z_tri = to_triton(np.empty((shape[0],), dtype=getattr(np, dtype_str)), device=device)
    kernel[(1,)](x_tri, z_tri, BLOCK_M=shape[0], BLOCK_N=shape[1], AXIS=axis)
    # numpy reference result
    z_ref = np.sum(x, axis=axis).astype(x.dtype)
    # compare
    np.testing.assert_allclose(z_ref, to_numpy(z_tri), rtol=0.01)

# ---------------
# test permute
# ---------------


@pytest.mark.parametrize("dtype_str, shape, perm",
                         [(dtype, shape, perm)
                          for dtype in ['float32']
                             for shape in [(128, 128)]
                             for perm in [(1, 0)]])
def test_permute(dtype_str, shape, perm, device='cuda'):

    # triton kernel
    @triton.jit
    def kernel(X, stride_xm, stride_xn,
               Z, stride_zm, stride_zn,
               BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr):
        off_m = tl.arange(0, BLOCK_M)
        off_n = tl.arange(0, BLOCK_N)
        Xs = X + off_m[:, None] * stride_xm + off_n[None, :] * stride_xn
        Zs = Z + off_m[:, None] * stride_zm + off_n[None, :] * stride_zn
        tl.store(Zs, tl.load(Xs))
    # input
    x = numpy_random(shape, dtype_str=dtype_str)
    # triton result
    z_tri = to_triton(np.empty_like(x), device=device)
    x_tri = to_triton(x, device=device)
    pgm = kernel[(1, 1)](x_tri, x_tri.stride(0), x_tri.stride(1),
                         z_tri, z_tri.stride(1), z_tri.stride(0),
                         BLOCK_M=shape[0], BLOCK_N=shape[1])
    # torch result
    z_ref = x.transpose(*perm)
    # compare
    triton.testing.assert_almost_equal(z_tri, z_ref)
    # parse ptx to make sure ld/st are vectorized
    ptx = pgm.asm['ptx']
    assert 'ld.global.v4' in ptx
    assert 'st.global.v4' in ptx

# ---------------
# test dot
# ---------------


@pytest.mark.parametrize("epilogue, allow_tf32, dtype",
                         [(epilogue, allow_tf32, dtype)
                          for epilogue in ['none', 'trans', 'add-matrix', 'add-rows', 'add-cols']
                          for allow_tf32 in [True, False]
                          for dtype in ['float32', 'int8']
                          if not (allow_tf32 and (dtype == 'int8'))])
def test_dot(epilogue, allow_tf32, dtype, device='cuda'):
    cc = _triton.runtime.cc(_triton.runtime.backend.CUDA, torch.cuda.current_device())
    if cc < 80:
        if dtype == 'int8':
            pytest.skip("Only test int8 on devices with sm >= 80")
        elif dtype == 'float32' and allow_tf32:
            pytest.skip("Only test tf32 on devices with sm >= 80")

    # triton kernel
    @triton.jit
    def kernel(X, stride_xm, stride_xk,
               Y, stride_yk, stride_yn,
               Z, stride_zm, stride_zn,
               BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr, BLOCK_K: tl.constexpr,
               ADD_MATRIX: tl.constexpr, ADD_ROWS: tl.constexpr, ADD_COLS: tl.constexpr,
               ALLOW_TF32: tl.constexpr):
        off_m = tl.arange(0, BLOCK_M)
        off_n = tl.arange(0, BLOCK_N)
        off_k = tl.arange(0, BLOCK_K)
        Xs = X + off_m[:, None] * stride_xm + off_k[None, :] * stride_xk
        Ys = Y + off_k[:, None] * stride_yk + off_n[None, :] * stride_yn
        Zs = Z + off_m[:, None] * stride_zm + off_n[None, :] * stride_zn
        z = tl.dot(tl.load(Xs), tl.load(Ys), allow_tf32=ALLOW_TF32)
        if ADD_MATRIX:
            z += tl.load(Zs)
        if ADD_ROWS:
            ZRs = Z + off_m * stride_zm
            z += tl.load(ZRs)[:, None]
        if ADD_COLS:
            ZCs = Z + off_n * stride_zn
            z += tl.load(ZCs)[None, :]
        tl.store(Zs, z)
    # input
    M, N, K = 64, 64, 32
    rs = RandomState(17)
    x = numpy_random((M, K), dtype_str=dtype, rs=rs)
    y = numpy_random((K, N), dtype_str=dtype, rs=rs)
    if allow_tf32:
        x = (x.view('uint32') & np.uint32(0xffffe000)).view('float32')
        y = (y.view('uint32') & np.uint32(0xffffe000)).view('float32')
    x_tri = to_triton(x, device=device)
    y_tri = to_triton(y, device=device)
    # triton result
    z = numpy_random((M, N), dtype_str=dtype, rs=rs)
    z_tri = to_triton(z, device=device)
    if epilogue == 'trans':
        z_tri = torch.as_strided(z_tri, (M, N), z_tri.stride()[::-1])
    pgm = kernel[(1, 1)](x_tri, x_tri.stride(0), x_tri.stride(1),
                         y_tri, y_tri.stride(0), y_tri.stride(1),
                         z_tri, z_tri.stride(0), z_tri.stride(1),
                         BLOCK_M=M, BLOCK_K=K, BLOCK_N=N,
                         ADD_MATRIX=epilogue == 'add-matrix',
                         ADD_ROWS=epilogue == 'add-rows',
                         ADD_COLS=epilogue == 'add-cols',
                         ALLOW_TF32=allow_tf32)
    # torch result
    z_ref = np.matmul(x, y)
    if epilogue == 'add-matrix':
        z_ref += z
    if epilogue == 'add-rows':
        z_ref += z[:, 0][:, None]
    if epilogue == 'add-cols':
        z_ref += z[0, :][None, :]
    # compare
    np.testing.assert_allclose(z_ref, to_numpy(z_tri), rtol=0.01)
    # make sure ld/st are vectorized
    ptx = pgm.asm['ptx']
    assert 'ld.global.v4' in ptx
    assert 'st.global.v4' in ptx
    if allow_tf32:
        assert 'mma.sync.aligned.m16n8k8.row.col.f32.tf32.tf32.f32' in ptx
    elif dtype == 'float32':
        assert 'mma.sync.aligned.m16n8k8.row.col.f32.tf32.tf32.f32' not in ptx
    elif dtype == 'int8':
        assert 'mma.sync.aligned.m16n8k32.row.col.satfinite.s32.s8.s8.s32' in ptx


def test_dot_without_load():
    @triton.jit
    def kernel(out):
        pid = tl.program_id(axis=0)
        a = tl.zeros((32, 32), tl.float32)
        b = tl.zeros((32, 32), tl.float32)
        c = tl.zeros((32, 32), tl.float32)
        c = tl.dot(a, b)
        pout = out + tl.arange(0, 32)[:, None] * 32 + tl.arange(0, 32)[None, :]
        tl.store(pout, c)

    out = torch.ones((32, 32), dtype=torch.float32, device="cuda")
    kernel[(1,)](out)

# ---------------
# test arange
# ---------------


@pytest.mark.parametrize("start", [0, 1, 7, 16])
def test_arange(start, device='cuda'):
    BLOCK = 128
    z_tri = torch.empty(BLOCK, dtype=torch.int32, device=device)

    @triton.jit
    def _kernel(z, BLOCK: tl.constexpr,
                START: tl.constexpr, END: tl.constexpr):
        off = tl.arange(0, BLOCK)
        val = tl.arange(START, END)
        tl.store(z + off, val)
    _kernel[(1,)](z_tri, START=start, END=start + BLOCK, BLOCK=BLOCK)
    z_ref = torch.arange(start, BLOCK + start, dtype=torch.int32, device=device)
    triton.testing.assert_almost_equal(z_tri, z_ref)

# ---------------
# test load
# ---------------
# 'bfloat16': torch.bfloat16,
# Testing masked loads with an intermate copy to shared memory run.


@pytest.mark.parametrize("dtype", [torch.float16, torch.float32])
def test_masked_load_shared_memory(dtype, device='cuda'):
    M = 32
    N = 32
    K = 8

    in1 = torch.rand((M, K), dtype=dtype, device=device)
    in2 = torch.rand((K, N), dtype=dtype, device=device)
    out = torch.zeros((M, N), dtype=dtype, device=device)

    @triton.jit
    def _kernel(in1_ptr, in2_ptr, output_ptr,
                in_stride, in2_stride, out_stride,
                in_numel, in2_numel, out_numel,
                M: tl.constexpr, N: tl.constexpr, K: tl.constexpr):

        M_offsets = tl.arange(0, M)
        N_offsets = tl.arange(0, N)
        K_offsets = tl.arange(0, K)

        in_offsets = M_offsets[:, None] * in_stride + K_offsets[None, :]
        in2_offsets = K_offsets[:, None] * in2_stride + N_offsets[None, :]

        # Load inputs.
        x = tl.load(in1_ptr + in_offsets, mask=in_offsets < in_numel)
        w = tl.load(in2_ptr + in2_offsets, mask=in2_offsets < in2_numel)

        # Without a dot product the memory doesn't get promoted to shared.
        o = tl.dot(x, w)

        # Store output
        output_offsets = M_offsets[:, None] * out_stride + N_offsets[None, :]
        tl.store(output_ptr + output_offsets, o, mask=output_offsets < in2_numel)

    pgm = _kernel[(1,)](in1, in2, out,
                        in1.stride()[0],
                        in2.stride()[0],
                        out.stride()[0],
                        in1.numel(),
                        in2.numel(),
                        out.numel(),
                        M=M, N=N, K=K)

    reference_out = torch.matmul(in1, in2)
    triton.testing.allclose(out, reference_out)


@pytest.mark.parametrize("cache", ["", ".ca", ".cg"])
def test_load_cache_modifier(cache):
    src = torch.empty(128, device='cuda')
    dst = torch.empty(128, device='cuda')

    @triton.jit
    def _kernel(dst, src, CACHE: tl.constexpr):
        offsets = tl.arange(0, 128)
        x = tl.load(src + offsets, cache_modifier=CACHE)
        tl.store(dst + offsets, x)

    pgm = _kernel[(1,)](dst, src, CACHE=cache)
    ptx = pgm.asm['ptx']
    if cache == '':
        assert 'ld.global.ca' not in ptx
        assert 'ld.global.cg' not in ptx
    if cache == '.cg':
        assert 'ld.global.cg' in ptx
        assert 'ld.global.ca' not in ptx
    if cache == '.ca':
        assert 'ld.global.ca' in ptx
        assert 'ld.global.cg' not in ptx

# ---------------
# test store
# ---------------

# ---------------
# test if
# ---------------

# ---------------
# test for
# ---------------

# ---------------
# test while
# ---------------

# ---------------
# test default
# ---------------
# TODO: can't be local to test_default


@triton.jit
def _impl(value=10):
    return value


def test_default():
    value = 5
    ret0 = torch.zeros(1, dtype=torch.int32, device='cuda')
    ret1 = torch.zeros(1, dtype=torch.int32, device='cuda')

    @triton.jit
    def _kernel(ret0, ret1, value):
        tl.store(ret0, _impl())
        tl.store(ret1, _impl(value))

    _kernel[(1,)](ret0, ret1, value)
    assert ret0.item() == 10
    assert ret1.item() == value

# ---------------
# test noop
# ----------------


def test_noop(device='cuda'):
    @triton.jit
    def kernel(x):
        pass
    x = to_triton(numpy_random((1,), dtype_str='int32'), device=device)
    kernel[(1, )](x)


@pytest.mark.parametrize("value, value_type", [
    (-1, 'i32'), (0, 'i32'), (1, None), (-2**31, 'i32'), (2**31 - 1, 'i32'),
    (2**31, 'u32'), (2**32 - 1, 'u32'), (2**32, 'i64'), (2**63 - 1, 'i64'),
    (-2**63, 'i64'), (2**63, 'u64'), (2**64 - 1, 'u64')
])
def test_value_specialization(value: int, value_type: str, device='cuda') -> None:

    @triton.jit
    def kernel(VALUE, X):
        pass

    x = torch.tensor([3.14159], device='cuda')
    pgm = kernel[(1, )](value, x)

    # Parse out the type of the 'VALUE' parameter from the Triton IR.
    triton_ir = pgm.asm['ttir']
    ir_value_match = re.match(r'\s*def void kernel\((\w+) VALUE ', triton_ir)
    ir_value_type = None if ir_value_match is None else ir_value_match.group(1)
    assert ir_value_type == value_type


@pytest.mark.parametrize(
    "value, overflow",
    [(2**64 - 1, False), (2**64, True), (-2**63, False), (-2**63 - 1, True)]
)
def test_value_specialization_overflow(value: int, overflow: bool, device='cuda') -> None:

    @triton.jit
    def kernel(VALUE, X):
        pass

    x = torch.tensor([3.14159], device='cuda')

    if overflow:
        with pytest.raises(RuntimeError, match='integer overflow'):
            kernel[(1, )](value, x)
    else:
        kernel[(1, )](value, x)
