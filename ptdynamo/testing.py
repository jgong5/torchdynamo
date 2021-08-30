import torch


def same(a, b):
    """ Check correctness to see if a and b match """
    if isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple))
        return all(same(ai, bi) for ai, bi in zip(a, b))
    elif isinstance(a, torch.Tensor):
        assert isinstance(b, torch.Tensor)
        return torch.allclose(a, b, atol=1e-4, rtol=1e-4)
    elif type(a).__name__ == "SquashedNormal":
        return same(a.mean, b.mean)
    else:
        raise RuntimeError(f"unsupported type: {type(a).__name__}")
