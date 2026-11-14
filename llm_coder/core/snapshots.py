"""
Custom local snapshot / diff-tree history system (no git).

Structure on disk, inside <project>/.agent/:
    manifest.json            -> {"head": "<snapshot_id or null>", "nodes": [...]}
    snapshots/<id>/meta.json -> {"id", "parent", "timestamp", "prompt", "files": [...]}
    snapshots/<id>/files/... -> full copies of files as they were BEFORE this
                                 snapshot was applied (i.e. restore targets),
                                 stored with the same relative path.
"""
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional, List, Dict


class SnapshotStore:
    def __init__(self, project_root: str):
        self.root = Path(project_root).resolve()
        self.agent_dir = self.root / ".agent"
        self.snapshots_dir = self.agent_dir / "snapshots"
        self.manifest_path = self.agent_dir / "manifest.json"
        self.agent_dir.mkdir(exist_ok=True)
        self.snapshots_dir.mkdir(exist_ok=True)
        if not self.manifest_path.exists():
            self._write_manifest({"head": None, "nodes": []})

    def _read_manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _write_manifest(self, data: dict) -> None:
        self.manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_snapshots(self) -> List[dict]:
        manifest = self._read_manifest()
        return manifest["nodes"]

    def head(self) -> Optional[str]:
        return self._read_manifest().get("head")

    def create_snapshot(self, prompt: str, changed_files: Dict[str, Optional[str]]) -> str:
        """
        changed_files: dict of {rel_path: previous_content_or_None}
        previous_content is what existed BEFORE the change (None if file was newly created).
        Stores those previous versions so we can revert.
        """
        snap_id = uuid.uuid4().hex[:12]
        snap_dir = self.snapshots_dir / snap_id
        files_dir = snap_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        stored_files = []
        for rel_path, prev_content in changed_files.items():
            stored_files.append({"path": rel_path, "existed_before": prev_content is not None})
            if prev_content is not None:
                target = files_dir / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(prev_content, encoding="utf-8")

        meta = {
            "id": snap_id,
            "parent": self.head(),
            "timestamp": time.time(),
            "prompt": prompt,
            "files": stored_files,
        }
        (snap_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

        manifest = self._read_manifest()
        manifest["nodes"].append(meta)
        manifest["head"] = snap_id
        self._write_manifest(manifest)
        return snap_id

    def get_meta(self, snap_id: str) -> dict:
        path = self.snapshots_dir / snap_id / "meta.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def restore_snapshot(self, snap_id: str, workspace) -> List[str]:
        """
        Revert the effects of this snapshot (and, implicitly, anything after it
        that touched the same files should be considered stale by the caller).
        Returns list of restored/removed file paths.
        """
        meta = self.get_meta(snap_id)
        snap_dir = self.snapshots_dir / snap_id
        files_dir = snap_dir / "files"
        touched = []
        for entry in meta["files"]:
            rel_path = entry["path"]
            if entry["existed_before"]:
                src = files_dir / rel_path
                content = src.read_text(encoding="utf-8", errors="replace")
                workspace.write_file(rel_path, content)
            else:
                # file did not exist before this snapshot -> remove it
                try:
                    workspace.delete_file(rel_path)
                except Exception:
                    pass
            touched.append(rel_path)

        manifest = self._read_manifest()
        # Only rewind head if we're actually restoring the current head
        # snapshot. Restoring an older snapshot mid-history must not
        # silently orphan every snapshot created after it by rewriting
        # head to point far earlier than reality.
        if manifest.get("head") == snap_id:
            manifest["head"] = meta.get("parent")
        self._write_manifest(manifest)
        return touched

    def restore_single_file(self, snap_id: str, rel_path: str, workspace) -> bool:
        files_dir = self.snapshots_dir / snap_id / "files"
        src = files_dir / rel_path
        if not src.exists():
            return False
        content = src.read_text(encoding="utf-8", errors="replace")
        workspace.write_file(rel_path, content)
        return True
