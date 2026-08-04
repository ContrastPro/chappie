from uuid import uuid4


class UUIDSnapshotIdFactory:
    def new_snapshot_id(self) -> str:
        return str(uuid4())


class UUIDShadowRunIdFactory:
    def new_shadow_run_id(self) -> str:
        return str(uuid4())
