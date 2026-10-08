"""Race-resistant Vault file operations, using directory descriptors where available."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pkm_framework.paths import VaultPathError, ensure_no_symlink_components, resolve_vault_path, validate_vault_relative


_DIR_FD_AVAILABLE = (
    os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
)


class VaultParentOpenError(VaultPathError):
    """A parent open failed and some created directories could not be removed."""

    def __init__(self, message: str, cleanup_paths: tuple[str, ...]):
        super().__init__(message)
        self.cleanup_paths = cleanup_paths


class StagedFileCleanupError(OSError):
    """A staging write failed and left a temporary file behind."""

    def __init__(self, message: str, residual_paths: tuple[str, ...]):
        super().__init__(message)
        self.residual_paths = residual_paths


@dataclass
class StagedFile:
    parent: SafeVaultParent
    temporary_name: str | None = None
    temporary_path: Path | None = None
    consumed: bool = False

    def cleanup(self) -> str | None:
        if self.consumed:
            return None
        temporary_name = self.temporary_name
        if temporary_name is None and self.temporary_path is not None:
            temporary_name = self.temporary_path.name
        if temporary_name is None:
            self.consumed = True
            return None
        parent_relative = PurePosixPath(self.parent.relative).parent
        residual_path = (parent_relative / temporary_name).as_posix()
        try:
            if self.temporary_name is not None and self.parent.fd is not None:
                os.unlink(self.temporary_name, dir_fd=self.parent.fd)
            elif self.temporary_name is not None:
                ensure_no_symlink_components(self.parent.path, self.parent.vault)
                (self.parent.path / self.temporary_name).unlink(missing_ok=True)
            elif self.temporary_path is not None:
                self.temporary_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, VaultPathError):
            return residual_path
        self.consumed = True
        return None


@dataclass
class SafeVaultParent:
    vault: Path
    relative: str
    path: Path
    name: str
    fd: int | None
    created_directories: tuple[str, ...] = ()

    @property
    def uses_dir_fd(self) -> bool:
        return self.fd is not None

    def close(self) -> None:
        if self.fd is not None:
            descriptor = self.fd
            self.fd = None
            try:
                os.close(descriptor)
            except OSError:
                # Closing a pinned directory must not turn an already-applied
                # write into an ambiguous failure response.
                pass

    def read_bytes(self) -> bytes | None:
        if self.fd is not None:
            try:
                file_fd = os.open(
                    self.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=self.fd,
                )
            except FileNotFoundError:
                return None
            try:
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    raise VaultPathError("Vault target is not a regular file.")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(file_fd, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(file_fd)

        target = self.path / self.name
        try:
            safe_target = resolve_vault_path(self.vault, self.relative, must_exist=False)
        except VaultPathError:
            raise
        if not safe_target.exists():
            return None
        try:
            return safe_target.read_bytes()
        except OSError as exc:
            raise VaultPathError("Cannot read Vault file safely.") from exc

    def stage(self, content: bytes) -> StagedFile:
        if self.fd is not None:
            for _ in range(10):
                temporary_name = f".{self.name}.pkm-{secrets.token_hex(8)}.tmp"
                try:
                    file_fd = os.open(
                        temporary_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=self.fd,
                    )
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("Could not allocate a temporary Vault file.")
            staged = StagedFile(self, temporary_name=temporary_name)
            try:
                with os.fdopen(file_fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                return staged
            except OSError:
                residual_path = staged.cleanup()
                if residual_path is not None:
                    raise StagedFileCleanupError(
                        "Could not remove a temporary Vault file after staging failed.",
                        (residual_path,),
                    ) from None
                raise

        ensure_no_symlink_components(self.path, self.vault)
        self.path.mkdir(parents=True, exist_ok=True)
        ensure_no_symlink_components(self.path, self.vault)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.name}.pkm-", suffix=".tmp", dir=self.path
        )
        staged = StagedFile(self, temporary_path=Path(temporary))
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            return staged
        except OSError:
            residual_path = staged.cleanup()
            if residual_path is not None:
                raise StagedFileCleanupError(
                    "Could not remove a temporary Vault file after staging failed.",
                    (residual_path,),
                ) from None
            raise

    def replace(self, staged: StagedFile) -> None:
        if staged.parent is not self:
            raise ValueError("Temporary file belongs to a different Vault directory.")
        if self.fd is not None and staged.temporary_name is not None:
            os.rename(
                staged.temporary_name,
                self.name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
            )
        elif staged.temporary_path is not None:
            ensure_no_symlink_components(self.path, self.vault)
            target = resolve_vault_path(self.vault, self.relative, must_exist=False)
            os.replace(staged.temporary_path, target)
        else:
            raise ValueError("Temporary Vault file is unavailable.")
        staged.consumed = True

    def unlink(self) -> None:
        if self.fd is not None:
            os.unlink(self.name, dir_fd=self.fd)
            return
        ensure_no_symlink_components(self.path, self.vault)
        target = resolve_vault_path(self.vault, self.relative, must_exist=True)
        target.unlink()


def open_vault_parent(vault: Path, relative: str, *, create: bool = False) -> SafeVaultParent:
    """Open a Vault-relative file's parent, pinning it with dir_fd on POSIX."""

    parsed = validate_vault_relative(relative)
    parent_parts = parsed.parts[:-1]
    name = parsed.parts[-1]
    parent_relative = "/".join(parent_parts)
    parent_path = vault.joinpath(*parent_parts)

    if _DIR_FD_AVAILABLE:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(vault, flags)
        created_directories: list[str] = []
        try:
            for index, component in enumerate(parent_parts):
                try:
                    child = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o755, dir_fd=descriptor)
                        created_directories.append("/".join(parent_parts[: index + 1]))
                    except FileExistsError:
                        pass
                    child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return SafeVaultParent(
                vault,
                parsed.as_posix(),
                parent_path,
                name,
                descriptor,
                tuple(created_directories),
            )
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            cleanup_paths = _remove_created_directories(vault, created_directories)
            if cleanup_paths:
                raise VaultParentOpenError(
                    "Could not open Vault parent and cleanup left directories behind.",
                    cleanup_paths,
                ) from None
            raise

    created_directories: list[str] = []
    try:
        parent = (
            resolve_vault_path(
                vault,
                parent_relative,
                must_exist=False,
                allow_directory=True,
            )
            if parent_parts
            else vault
        )
        if not create and not parent.exists():
            raise FileNotFoundError(parent_relative)
        if parent.exists() and not parent.is_dir():
            raise VaultPathError("Vault file parent is not a directory.")
        if create:
            missing: list[str] = []
            current = parent
            while current != vault and not current.exists():
                missing.append(current.relative_to(vault).as_posix())
                current = current.parent
            for relative_directory in reversed(missing):
                directory = vault / relative_directory
                ensure_no_symlink_components(directory.parent, vault)
                try:
                    directory.mkdir()
                except FileExistsError:
                    if not directory.is_dir():
                        raise VaultPathError("Vault parent is not a directory.")
                else:
                    created_directories.append(relative_directory)
                ensure_no_symlink_components(directory, vault)
    except FileNotFoundError as exc:
        cleanup_paths = _remove_created_directories(vault, created_directories)
        if cleanup_paths:
            raise VaultParentOpenError(
                "Could not safely open Vault parent and cleanup left directories behind.",
                cleanup_paths,
            ) from exc
        raise
    except (OSError, VaultPathError) as exc:
        cleanup_paths = _remove_created_directories(vault, created_directories)
        if cleanup_paths:
            raise VaultParentOpenError(
                "Could not safely open Vault parent and cleanup left directories behind.",
                cleanup_paths,
            ) from exc
        raise VaultPathError("Cannot safely open Vault file parent.") from exc
    return SafeVaultParent(vault, parsed.as_posix(), parent, name, None, tuple(created_directories))


def remove_empty_vault_directory(vault: Path, relative: str) -> bool:
    """Remove one empty Vault-relative directory without following symlinks."""

    try:
        parent = open_vault_parent(vault, relative)
    except FileNotFoundError:
        return True
    except (OSError, VaultPathError):
        return False
    try:
        if parent.fd is not None:
            os.rmdir(parent.name, dir_fd=parent.fd)
        else:
            target = resolve_vault_path(
                vault, relative, must_exist=True, allow_directory=True
            )
            target.rmdir()
        return True
    except FileNotFoundError:
        return True
    except (OSError, VaultPathError):
        return False
    finally:
        parent.close()


def _remove_created_directories(vault: Path, directories: list[str]) -> tuple[str, ...]:
    remaining = [
        path
        for path in reversed(directories)
        if not remove_empty_vault_directory(vault, path)
    ]
    return tuple(remaining)
