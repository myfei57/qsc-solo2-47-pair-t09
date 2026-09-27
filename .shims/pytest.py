"""Tiny pytest stand-in for run_tests.py: only what the test suite imports.

This shim lives outside the package and is only placed on sys.path by the
stand-in runner, because pytest cannot be installed in this environment.
"""


class _Raises:
    def __init__(self, expected):
        self.expected = expected

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"expected {self.expected.__name__} to be raised")
        if issubclass(exc_type, self.expected):
            return True
        return False


def raises(expected):
    return _Raises(expected)


def fixture(*args, **kwargs):
    def decorate(fn):
        return fn

    if args and callable(args[0]):
        return args[0]
    return decorate


class mark:  # noqa: D101 - stand-in namespace
    @staticmethod
    def parametrize(*args, **kwargs):
        def decorate(fn):
            return fn

        return decorate
