"""Compatibility entry point for the Incus compute driver."""


def __getattr__(name):
    if name == "IncusDriver":
        from nova.virt.incus.driver import IncusDriver

        return IncusDriver
    raise AttributeError(name)
