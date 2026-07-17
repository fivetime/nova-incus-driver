"""Compatibility entry point for the Incus compute driver."""


def __getattr__(name):
    if name == "LXDDriver":
        from nova.virt.lxd.driver import LXDDriver

        return LXDDriver
    raise AttributeError(name)
