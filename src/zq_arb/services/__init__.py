"""Application services and deterministic state ownership.

Import concrete services from their modules. Keeping this package initializer free
of eager imports prevents the execution coordinator and runtime from forming an
order-dependent import cycle.
"""
