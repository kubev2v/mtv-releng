import glob
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class GoCommandError(RuntimeError):
    """A go / govulncheck command exited non-zero."""


def decode_json_stream(text: str) -> list[dict]:
    """Split the concatenated JSON objects `go list -json` emits."""
    decoder = json.JSONDecoder()
    objects, idx = [], 0
    while idx < len(text):
        if text[idx].isspace():
            idx += 1
            continue
        obj, idx = decoder.raw_decode(text, idx)
        objects.append(obj)
    return objects


class Go:
    """Runs go tooling inside one module directory.

    Module-graph commands (get, tidy, list) use -mod=mod; only builds use the
    vendor directory, matching how the downstream Containerfiles build.
    """

    def __init__(self, module_dir: str, timeout: int, vendor: bool = False):
        self.module_dir = module_dir
        self.timeout = timeout
        self.vendor = vendor

    def _run(self, args: list[str], goflags: str) -> str:
        env = {**os.environ, "GOFLAGS": goflags, "GOWORK": "off"}
        logger.info(f"[{self.module_dir}] {' '.join(args)}")
        try:
            result = subprocess.run(
                args,
                cwd=self.module_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise GoCommandError(f"{' '.join(args)} timed out") from e
        if result.returncode != 0:
            raise GoCommandError(
                result.stderr.strip() or result.stdout.strip()
            )
        return result.stdout

    def _mod(self, *args: str) -> str:
        return self._run(["go", *args], "-mod=mod")

    def build(self, target: str, out: str = "") -> None:
        """Build like Konflux: from vendor/, or read-only go.mod/go.sum."""
        mod = "-mod=vendor" if self.vendor else "-mod=readonly"
        if not out and not target.startswith("./"):
            out = os.devnull
        self._run(
            ["go", "build", *(["-o", out] if out else []), target],
            f"{mod} -tags=strictfipsruntime",
        )

    def fill_sums(self, target: str) -> None:
        """Record every go.sum entry building *target* needs (no tidy)."""
        self._run(
            ["go", "build", "-o", os.devnull, target],
            "-mod=mod -tags=strictfipsruntime",
        )

    def edit_json(self) -> dict:
        return json.loads(self._mod("mod", "edit", "-json"))

    def get(self, specs: list[str]) -> None:
        self._mod("get", *specs)

    def tidy(self) -> None:
        self._mod("mod", "tidy")

    def vendor_modules(self) -> None:
        self._mod("mod", "vendor")

    def set_toolchain(self, toolchain: str) -> None:
        self._mod("mod", "edit", f"-toolchain={toolchain or 'none'}")

    def list_updates(self) -> list[dict]:
        return decode_json_stream(
            self._mod("list", "-m", "-u", "-json", "all")
        )

    def versions(self, path: str) -> list[str]:
        out = self._mod("list", "-m", "-versions", path).split()
        return out[1:]  # first field is the module path

    def govulncheck(self, binary: str = "") -> list[dict]:
        """Module-level vulnerability findings for this module.

        With *binary*, scan that built executable instead of the module's
        packages; for a module that only pins a tool (.konflux/validation
        builds OPA and has no Go files) that's the only thing to scan.
        """
        env = {**os.environ, "GOFLAGS": "-mod=mod", "GOWORK": "off"}
        args = ["govulncheck", "-scan", "module", "-format", "json"]
        if binary:
            args[1:1] = ["-mode", "binary"]
            args.append(binary)
        # -scan module walks the import graph of the "." package and rejects
        # patterns, so it fails when the module directory itself has no Go
        # files (forklift's root). Drop in a stub main importing every
        # non-internal package of the module's build closure — the union of
        # their import graphs — and remove it afterwards. Internal packages
        # can't be imported from here but every one sits behind a non-internal
        # package of its own module, so coverage is preserved.
        stub = ""
        if not binary and not glob.glob(os.path.join(self.module_dir, "*.go")):
            stub = os.path.join(self.module_dir, "zz_dep_bump_scan.go")
            template = (
                '{{if and (ne .Name "main") .Module}}{{.ImportPath}}{{end}}'
            )
            packages = self._run(
                ["go", "list", "-deps", "-f", template, "./..."], "-mod=mod"
            )
            importable = [
                p
                for p in packages.splitlines()
                if p and not p.endswith("/internal") and "/internal/" not in p
            ]
            with open(stub, "w") as f:
                f.write("package main\n\n")
                for p in importable:
                    f.write(f'import _ "{p}"\n')
                f.write("\nfunc main() {}\n")
        logger.info(f"[{self.module_dir}] {' '.join(args)}")
        try:
            result = subprocess.run(
                args,
                cwd=self.module_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise GoCommandError(f"{' '.join(args)} timed out") from e
        finally:
            if stub:
                os.unlink(stub)
        # govulncheck exits 3 when it finds vulnerabilities; that's success.
        if result.returncode not in (0, 3):
            raise GoCommandError(result.stderr.strip())
        return decode_json_stream(result.stdout)
