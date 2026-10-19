"""nc-perform: a thin hosted wrapper around the public NetComposer Performer kit.

The kit (ncperf) still interprets, renders, and writes the record. This package
adds the job API, storage, queue, limits, and Ed25519 signing. It versions
with the kit in this repository.
"""
from ncperf import __version__ as KIT_VERSION

__version__ = KIT_VERSION
