"""Language-neutral conformance runner for the Node <-> Core contract.

The fixtures under contract/scenarios are plain data; this package only loads them, sends the HTTP requests they describe to
a core that is already listening, and compares what came back. Nothing in the fixtures or the schemas is specific to Python:
a Rust core is checked by the same files. See contract/README.md.
"""
