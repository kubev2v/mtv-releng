"""Tests for the dep_bump pipeline helpers."""

import os
from argparse import Namespace
from unittest.mock import MagicMock, patch

from models.dto import DepBumpRepoResultDTO, GoBumpDTO, ImageBumpDTO
from tasks import dep_bump as db
from tasks.dep_bump import ModFile
from wrappers.go import GoCommandError, decode_json_stream

from pipelines import dep_bump as pipeline

# --- tags -----------------------------------------------------------------------

GO_TOOLSET_TAGS = [
    "1.25.9-1778604137",
    "1.25.9-1778675823",
    "1.25.9-1778675823-source",
    "1.25.10-1783614278",
    "1.26.1-1790000000",
    "9.7-1778675823",
    "latest",
]


def test_newest_in_stream_keeps_go_minor_and_skips_rhel_and_source_tags():
    assert (
        db.newest_in_stream("1.25.9-1778604137", GO_TOOLSET_TAGS)
        == "1.25.10-1783614278"
    )


def test_newest_in_stream_keeps_rhel_minor():
    tags = ["9.7-1778562320", "9.7-1779770818", "9.8-1790000000"]
    assert db.newest_in_stream("9.7-1778562320", tags) == "9.7-1779770818"


def test_newest_in_stream_single_part_version():
    tags = ["1-1756959223", "1-1792454849", "9.7-1779165855"]
    assert db.newest_in_stream("1-1756959223", tags) == "1-1792454849"


def test_newest_in_stream_openshift_tag():
    tags = [
        "v4.18.0-202504021503.p0.gbd5e2c9.assembly.stream.el9",
        "v4.18.0-202610071009.p2.gbc7aeec.assembly.stream.el9",
        "v4.18.0-202610071009.p2.gbc7aeec.assembly.stream.el9-source",
        "v4.19.0-202610100000.p2.gabc1234.assembly.stream.el9",
    ]
    assert db.newest_in_stream(tags[0], tags) == tags[1]


def test_newest_in_stream_unknown_tag_format():
    assert db.newest_in_stream("latest", ["latest"]) is None


def test_newest_ocp_tag_and_stream_labels():
    stream, el = db.ocp_stream(
        "v4.16.0", "202607311921.p2.g6f88b58.assembly.stream.el9"
    )
    assert (stream, el) == ("4.16", "el9")
    tags = [
        "v4.16.0-202607311921.p2.g6f88b58.assembly.stream.el9",
        "v4.16.0-202610011108.p2.g33afbae.assembly.stream.el9",
        "v4.20.0-202610011108.p2.g33afbae.assembly.stream.el9",
    ]
    assert db.newest_ocp_tag(tags, "4.16", "el9") == tags[1]


def test_go_toolset_for_directive_stays_on_its_minor():
    assert db.go_toolset_for("1.25.0", GO_TOOLSET_TAGS) == "1.25.10-1783614278"
    assert db.go_toolset_for("1.27.0", GO_TOOLSET_TAGS) is None


def test_go_toolset_version_only_for_go_toolset_images():
    assert db.go_toolset_version(
        "registry.redhat.io/ubi8/go-toolset:1.24.6-1764274816"
    ) == (1, 24, 6)
    assert (
        db.go_toolset_version("registry.redhat.io/ubi9-minimal:9.7-1") is None
    )


# --- Containerfiles -------------------------------------------------------------

CONTAINERFILE = """\
FROM registry.redhat.io/ubi9/go-toolset:1.25.9-1778604137 AS builder
RUN make build
FROM --platform=linux/amd64 registry.redhat.io/ubi9-minimal:9.7-1778562320
COPY --from=builder /app /app
"""


def test_parse_from_lines_and_rewrite():
    lines = db.parse_from_lines(CONTAINERFILE)
    assert [(ln.index, ln.stage) for ln in lines] == [(0, "builder"), (2, "")]
    assert lines[1].ref == "registry.redhat.io/ubi9-minimal:9.7-1778562320"
    new = db.rewrite_ref(
        CONTAINERFILE, 2, lines[1].ref, "registry.redhat.io/ubi9-minimal:9.7-2"
    )
    assert (
        "--platform=linux/amd64 registry.redhat.io/ubi9-minimal:9.7-2" in new
    )
    assert new.splitlines()[0] == CONTAINERFILE.splitlines()[0]


def test_split_ref():
    assert db.split_ref("registry.redhat.io/ubi9/ubi:9.7-1") == (
        "registry.redhat.io/ubi9/ubi",
        "9.7-1",
        "",
    )
    assert db.split_ref("registry.redhat.io/o/ose-cli@sha256:ab") == (
        "registry.redhat.io/o/ose-cli",
        "",
        "sha256:ab",
    )
    assert db.split_ref("localhost:5000/img") == ("localhost:5000/img", "", "")


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def test_rpm_components_matches_containerfile_and_stage(tmp_path):
    root = str(tmp_path)
    _write(
        root,
        ".konflux/api/rpms.in.yaml",
        "context:\n  containerfile:\n"
        "    file: ../../build/api/Containerfile-downstream\n",
    )
    _write(
        root,
        ".konflux/v2v/appliance/rpms.in.yaml",
        "context:\n  containerfile:\n"
        "    file: ../../../build/v2v/Containerfile-downstream\n"
        "    stageName: appliance\n",
    )
    _write(
        root,
        ".konflux/v2v/runtime/rpms.in.yaml",
        "context:\n  containerfile:\n"
        "    file: ../../../build/v2v/Containerfile-downstream\n"
        "    stageName: runtime\n",
    )
    changed = {
        "build/api/Containerfile-downstream": {""},
        "build/v2v/Containerfile-downstream": {"appliance"},
    }
    assert db.rpm_components(root, ".konflux/**/rpms.in.yaml", changed) == [
        ".konflux/api",
        ".konflux/v2v/appliance",
    ]


def test_containerfiles_glob_and_exclude(tmp_path):
    root = str(tmp_path)
    for rel in [
        "build/api/Containerfile-downstream",
        "build/v2v/Containerfile-downstream-fssupport",
        "build/forklift-operator-bundle/Containerfile-downstream",
        "build/api/Containerfile",
    ]:
        _write(root, rel, "FROM x\n")
    assert db.containerfiles(
        root,
        ["build/**/Containerfile-downstream*"],
        ["build/forklift-operator-bundle/"],
    ) == [
        "build/api/Containerfile-downstream",
        "build/v2v/Containerfile-downstream-fssupport",
    ]


# --- Go candidates and guards ---------------------------------------------------

GOVULNCHECK = [
    {"config": {"scanner_name": "govulncheck"}},
    {"osv": {"id": "GO-2026-5970", "aliases": ["CVE-2026-56852"]}},
    {"osv": {"id": "GO-2026-1111", "aliases": ["CVE-2026-1"]}},
    {"osv": {"id": "GO-2026-2222", "aliases": []}},
    {
        "finding": {
            "osv": "GO-2026-5970",
            "fixed_version": "v0.39.0",
            "trace": [{"module": "golang.org/x/text", "version": "v0.36.0"}],
        }
    },
    {
        "finding": {
            "osv": "GO-2026-1111",
            "fixed_version": "v0.38.0",
            "trace": [{"module": "golang.org/x/text", "version": "v0.36.0"}],
        }
    },
    {
        "finding": {
            "osv": "GO-2026-2222",
            "trace": [{"module": "example.com/dead", "version": "v1.0.0"}],
        }
    },
    {
        "finding": {
            "osv": "GO-2026-3333",
            "fixed_version": "v1.25.10",
            "trace": [{"module": "stdlib", "version": "v1.25.9"}],
        }
    },
]


def test_security_candidates_take_highest_fix_and_skip_stdlib():
    cands, unfixable = db.security_candidates(GOVULNCHECK)
    assert list(cands) == ["golang.org/x/text"]
    text = cands["golang.org/x/text"]
    assert text.version == "v0.39.0"
    assert text.ids == [
        "GO-2026-5970",
        "CVE-2026-56852",
        "GO-2026-1111",
        "CVE-2026-1",
    ]
    assert unfixable == ["example.com/dead: GO-2026-2222"]


def test_patch_candidates_direct_same_minor_only():
    modules = [
        {"Path": "github.com/kubev2v/forklift", "Main": True},
        {
            "Path": "k8s.io/api",
            "Version": "v0.32.5",
            "Update": {"Version": "v0.32.13"},
        },
        {
            "Path": "k8s.io/client-go",
            "Version": "v0.32.5",
            "Update": {"Version": "v0.35.0"},
        },
        {
            "Path": "golang.org/x/net",
            "Version": "v0.43.0",
            "Indirect": True,
            "Update": {"Version": "v0.43.1"},
        },
        {
            "Path": "github.com/gophercloud/gophercloud",
            "Version": "v1.2.0",
            "Replace": {"Path": "github.com/fork/gophercloud"},
            "Update": {"Version": "v1.2.1"},
        },
        {
            "Path": "example.com/pseudo",
            "Version": "v0.0.0-20240101000000-abcdefabcdef",
            "Update": {"Version": "v0.0.1"},
        },
        {
            "Path": "golang.org/x/text",
            "Version": "v0.36.0",
            "Update": {"Version": "v0.36.1"},
        },
    ]
    cands = db.patch_candidates(modules, skip={"golang.org/x/text"})
    assert [(c.path, c.version) for c in cands] == [("k8s.io/api", "v0.32.13")]


def _mod(go="1.25.0", **requires):
    return ModFile(
        go=go,
        toolchain="",
        requires={
            p.replace("__", "/"): (v, False) for p, v in requires.items()
        },
        replaced=set(),
    )


GUARD = ["k8s.io/", "kubevirt.io/"]


def test_cascades_flags_guarded_minor_and_any_major():
    before = ModFile(
        "1.24.0",
        "",
        {
            "go.mongodb.org/mongo-driver": ("v1.17.6", True),
            "k8s.io/api": ("v0.32.13", False),
            "golang.org/x/crypto": ("v0.41.0", False),
            "github.com/foo/bar": ("v1.4.0", True),
        },
        set(),
    )
    after = ModFile(
        "1.24.0",
        "",
        {
            "go.mongodb.org/mongo-driver": ("v1.17.10", True),
            "k8s.io/api": ("v0.35.0", False),
            "golang.org/x/crypto": ("v0.53.0", False),
            "github.com/foo/bar": ("v2.0.0", True),
        },
        set(),
    )
    moved = db.cascades(before, after, {"go.mongodb.org/mongo-driver"}, GUARD)
    assert moved == [
        "k8s.io/api v0.32.13 -> v0.35.0",
        "github.com/foo/bar v1.4.0 -> v2.0.0",
    ]


def test_cascades_allows_guarded_patch_moves():
    before = ModFile("1.25.0", "", {"k8s.io/api": ("v0.32.5", False)}, set())
    after = ModFile("1.25.0", "", {"k8s.io/api": ("v0.32.13", False)}, set())
    assert db.cascades(before, after, set(), GUARD) == []


def test_parse_mod_json():
    data = {
        "Go": "1.25.0",
        "Toolchain": "go1.25.9",
        "Require": [
            {"Path": "a", "Version": "v1.0.0"},
            {"Path": "b", "Version": "v2.0.0", "Indirect": True},
        ],
        "Replace": [{"Old": {"Path": "a"}, "New": {"Path": "../a"}}],
    }
    mod = db.parse_mod_json(data)
    assert mod.go == "1.25.0" and mod.toolchain == "go1.25.9"
    assert mod.requires == {"a": ("v1.0.0", False), "b": ("v2.0.0", True)}
    assert mod.replaced == {"a"}


def test_decode_json_stream():
    text = '{"a": 1}\n{"b": {"c": 2}}\n'
    assert decode_json_stream(text) == [{"a": 1}, {"b": {"c": 2}}]


# --- update_module (mocked go) --------------------------------------------------


class FakeGo:
    """Simulates go get/tidy/build against an in-memory go.mod."""

    def __init__(self, base: ModFile, effects: dict, broken: set[str]):
        self.base = base
        self.effects = effects  # spec -> {path: version} it also moves
        self.broken = broken  # specs whose presence breaks the build
        self.state = None
        self.applied: list[str] = []
        self.reset()

    def reset(self):
        self.state = ModFile(
            self.base.go, self.base.toolchain, dict(self.base.requires), set()
        )
        self.applied = []

    def edit_json(self):
        return {
            "Go": self.state.go,
            "Toolchain": self.state.toolchain,
            "Require": [
                {"Path": p, "Version": v, "Indirect": i}
                for p, (v, i) in self.state.requires.items()
            ],
        }

    def get(self, specs):
        for spec in specs:
            path, version = spec.split("@")
            self.state.requires[path] = (version, False)
            for p, v in self.effects.get(spec, {}).items():
                if p == "go":
                    self.state.go = v
                else:
                    self.state.requires[p] = (v, False)
            self.applied.append(spec)

    def tidy(self):
        pass

    def vendor_modules(self):
        pass

    def set_toolchain(self, toolchain):
        self.state.toolchain = toolchain

    def build(self, target):
        if any(s in self.broken for s in self.applied):
            raise GoCommandError("undefined: foo.Bar")

    def govulncheck(self):
        return []

    def list_updates(self):
        return []

    def versions(self, path):
        return []


def _module(fake: FakeGo, builders=None, allow_raise=False):
    run = MagicMock()
    run.cfg = {"cascade_guard": GUARD, "max_single_attempts": 20}
    module = pipeline._Module.__new__(pipeline._Module)
    module.run = run
    module.rel = "."
    module.cfg = {"build": "./..."}
    module.vendor = True
    module.go = fake
    module.paths = []
    module.builders = builders or []
    module.resync = False
    module.broken = ""
    module.restore = fake.reset
    module.allow_go_raise = lambda: allow_raise
    module.builder_bumps = lambda directive: []
    return module


def test_update_module_keeps_safe_bumps_and_rejects_cascade_and_breakage():
    base = _mod(
        **{
            "golang.org__x__text": "v0.36.0",
            "k8s.io__api": "v0.32.5",
            "go.mongodb.org__mongo-driver": "v1.17.6",
            "github.com__foo__bar": "v1.2.0",
        }
    )
    fake = FakeGo(
        base,
        effects={
            "go.mongodb.org/mongo-driver@v1.17.10": {"k8s.io/api": "v0.35.0"}
        },
        broken={"github.com/foo/bar@v1.2.9"},
    )
    module = _module(fake)
    fake.govulncheck = lambda: [
        {"osv": {"id": "GO-2026-5970", "aliases": ["CVE-2026-56852"]}},
        {
            "finding": {
                "osv": "GO-2026-5970",
                "fixed_version": "v0.39.0",
                "trace": [{"module": "golang.org/x/text"}],
            }
        },
        {"osv": {"id": "GO-2026-9", "aliases": []}},
        {
            "finding": {
                "osv": "GO-2026-9",
                "fixed_version": "v1.17.10",
                "trace": [{"module": "go.mongodb.org/mongo-driver"}],
            }
        },
    ]
    fake.list_updates = lambda: [
        {
            "Path": "github.com/foo/bar",
            "Version": "v1.2.0",
            "Update": {"Version": "v1.2.9"},
        },
        {
            "Path": "k8s.io/api",
            "Version": "v0.32.5",
            "Update": {"Version": "v0.32.13"},
        },
    ]
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")

    assert pipeline.update_module(module, result) is True

    kept = {(b.path, b.new, b.kind) for b in result.go_bumps}
    assert ("golang.org/x/text", "v0.39.0", "security") in kept
    assert ("k8s.io/api", "v0.32.13", "patch") in kept
    reasons = {r.path: r.reason for r in result.rejected}
    assert reasons["go.mongodb.org/mongo-driver"].startswith(
        "cascades: k8s.io/api"
    )
    assert reasons["github.com/foo/bar"].startswith("build fails")
    # The module ends in the accepted state.
    assert fake.state.requires["golang.org/x/text"][0] == "v0.39.0"
    assert fake.state.requires["go.mongodb.org/mongo-driver"][0] == "v1.17.6"


def test_update_module_go_raise_only_when_allowed():
    base = _mod(go="1.24.6", **{"google.golang.org__grpc": "v1.74.2"})
    effects = {"google.golang.org/grpc@v1.82.2": {"go": "1.25.0"}}
    vuln = [
        {"osv": {"id": "GO-2026-6443", "aliases": ["CVE-2026-84445"]}},
        {
            "finding": {
                "osv": "GO-2026-6443",
                "fixed_version": "v1.82.2",
                "trace": [{"module": "google.golang.org/grpc"}],
            }
        },
    ]

    fake = FakeGo(base, effects, set())
    fake.govulncheck = lambda: vuln
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is False
    assert "needs go 1.25.0" in result.rejected[0].reason

    fake = FakeGo(base, effects, set())
    fake.govulncheck = lambda: vuln
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert (
        pipeline.update_module(_module(fake, allow_raise=True), result) is True
    )
    kinds = {(b.path, b.kind) for b in result.go_bumps}
    assert ("go", "go-directive") in kinds
    assert ("google.golang.org/grpc", "security") in kinds


def test_update_module_skips_module_that_is_not_tidy_clean():
    base = _mod(**{"k8s.io__api": "v0.32.5"})
    fake = FakeGo(base, {}, set())

    def drifting_tidy():
        fake.state.requires["k8s.io/api"] = ("v0.35.0", False)

    fake.tidy = drifting_tidy
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.10")
    assert pipeline.update_module(_module(fake), result) is False
    assert "not tidy-clean" in result.rejected[0].reason


def test_update_module_skips_module_with_stale_vendor():
    # kubectl-mtv on 2.12: its vendor/ has an older copy of forklift's own
    # packages, so re-vendoring alone breaks the build.
    fake = FakeGo(_mod(), {}, set())
    synced = []
    fake.vendor_modules = lambda: synced.append(True)
    real_build = fake.build

    def build(target):
        if synced:
            raise GoCommandError(
                "x.go:55: has no field DedicatedMigrationHosts"
            )
        real_build(target)

    fake.build = build
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is False
    assert result.rejected[0].reason.startswith(
        "re-running `go mod tidy`/`go mod vendor` with no bump breaks"
    )


def test_security_target_moves_to_latest_patch_of_fixed_minor():
    base = _mod(**{"go.mongodb.org__mongo-driver": "v1.17.2"})
    fake = FakeGo(base, {}, set())
    fake.govulncheck = lambda: [
        {"osv": {"id": "GO-2026-5327", "aliases": ["CVE-2026-2303"]}},
        {
            "finding": {
                "osv": "GO-2026-5327",
                "fixed_version": "v1.17.7",
                "trace": [{"module": "go.mongodb.org/mongo-driver"}],
            }
        },
    ]
    fake.versions = lambda path: [
        "v1.17.6",
        "v1.17.7",
        "v1.17.10",
        "v1.18.0",
        "v1.17.11-rc1",
    ]
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is True
    [bump] = [b for b in result.go_bumps if b.kind == "security"]
    assert (bump.old, bump.new) == ("v1.17.2", "v1.17.10")


def test_patch_candidates_are_listed_after_security_bumps():
    # x/text 0.41 drags x/sys to 0.47; a patch bump to x/sys 0.44 computed
    # up front would conflict, so patches come from the post-security state.
    base = _mod(
        **{"golang.org__x__text": "v0.36.0", "golang.org__x__sys": "v0.43.0"}
    )
    fake = FakeGo(
        base,
        {"golang.org/x/text@v0.41.0": {"golang.org/x/sys": "v0.47.0"}},
        set(),
    )
    fake.govulncheck = lambda: [
        {"osv": {"id": "GO-2026-5970", "aliases": []}},
        {
            "finding": {
                "osv": "GO-2026-5970",
                "fixed_version": "v0.41.0",
                "trace": [{"module": "golang.org/x/text"}],
            }
        },
    ]

    def list_updates():
        current = fake.state.requires["golang.org/x/sys"][0]
        return [
            {
                "Path": "golang.org/x/sys",
                "Version": current,
                "Update": {"Version": "v0.44.0"},
            }
        ]

    fake.list_updates = list_updates
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is True
    assert result.rejected == []
    assert fake.state.requires["golang.org/x/sys"][0] == "v0.47.0"


def test_conflicting_security_targets_are_raised_to_what_go_requires():
    # Real 2.12 vsphere case: x/sys's fix is v0.44.0 but x/text's fix
    # (v0.41.0) needs x/sys >= v0.47.0; exact pins make `go get` fail.
    base = _mod(
        **{"golang.org__x__text": "v0.36.0", "golang.org__x__sys": "v0.43.0"}
    )
    fake = FakeGo(base, {}, set())
    real_get = fake.get

    def get(specs):
        if "golang.org/x/sys@v0.44.0" in specs and any(
            s.startswith("golang.org/x/text@") for s in specs
        ):
            raise GoCommandError(
                "go: golang.org/x/text@v0.41.0 indirectly requires "
                "golang.org/x/sys@v0.47.0, not golang.org/x/sys@v0.44.0"
            )
        real_get(specs)

    fake.get = get
    fake.govulncheck = lambda: [
        {"osv": {"id": "GO-2026-5024", "aliases": []}},
        {
            "finding": {
                "osv": "GO-2026-5024",
                "fixed_version": "v0.44.0",
                "trace": [{"module": "golang.org/x/sys"}],
            }
        },
        {"osv": {"id": "GO-2026-5970", "aliases": []}},
        {
            "finding": {
                "osv": "GO-2026-5970",
                "fixed_version": "v0.41.0",
                "trace": [{"module": "golang.org/x/text"}],
            }
        },
    ]
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is True
    assert result.rejected == []
    new = {b.path: b.new for b in result.go_bumps}
    assert new == {
        "golang.org/x/sys": "v0.47.0",
        "golang.org/x/text": "v0.41.0",
    }


def test_failed_combined_attempt_does_not_inflate_single_retries():
    # x/crypto's fix needs go 1.26 and drags x/sys up; once the combined set
    # fails, x/sys must be retried at its own target, not the dragged one.
    base = _mod(
        **{"golang.org__x__crypto": "v0.50.0", "golang.org__x__sys": "v0.43.0"}
    )
    fake = FakeGo(
        base, {"golang.org/x/crypto@v0.56.0": {"go": "1.26.0"}}, set()
    )
    real_get = fake.get

    def get(specs):
        if "golang.org/x/crypto@v0.56.0" in specs and (
            "golang.org/x/sys@v0.44.0" in specs
        ):
            raise GoCommandError(
                "go: golang.org/x/crypto@v0.56.0 requires "
                "golang.org/x/sys@v0.48.0, not golang.org/x/sys@v0.44.0"
            )
        real_get(specs)

    fake.get = get
    fake.govulncheck = lambda: [
        {"osv": {"id": "GO-1", "aliases": []}},
        {
            "finding": {
                "osv": "GO-1",
                "fixed_version": "v0.44.0",
                "trace": [{"module": "golang.org/x/sys"}],
            }
        },
        {"osv": {"id": "GO-2", "aliases": []}},
        {
            "finding": {
                "osv": "GO-2",
                "fixed_version": "v0.56.0",
                "trace": [{"module": "golang.org/x/crypto"}],
            }
        },
    ]
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is True
    assert {b.path: b.new for b in result.go_bumps} == {
        "golang.org/x/sys": "v0.44.0"
    }
    [rejected] = result.rejected
    assert rejected.path == "golang.org/x/crypto"
    assert "needs go 1.26.0" in rejected.reason


def test_govulncheck_binary_mode_args_and_exit_code_3_is_success():
    from wrappers.go import Go

    done = MagicMock(returncode=3, stdout='{"config": {}}', stderr="")
    with patch("wrappers.go.subprocess.run", return_value=done) as m_run:
        assert Go("/m", 60).govulncheck(binary="/tmp/opa") == [{"config": {}}]
    assert m_run.call_args.args[0] == [
        "govulncheck",
        "-mode",
        "binary",
        "-scan",
        "module",
        "-format",
        "json",
        "/tmp/opa",
    ]


def test_tool_module_scans_its_binary_and_is_never_tidied():
    # .konflux/validation only pins OPA: tidy would drop every requirement.
    go = MagicMock()
    go.govulncheck.return_value = []
    module = pipeline._Module.__new__(pipeline._Module)
    module.go = go
    module.vendor = False
    module.cfg = {"build": "github.com/open-policy-agent/opa", "tidy": False}
    module.scan()
    out = go.build.call_args.kwargs["out"]
    go.govulncheck.assert_called_once_with(binary=out)
    module.sync()
    go.tidy.assert_not_called()
    go.fill_sums.assert_called_once_with("github.com/open-policy-agent/opa")


def test_go_build_matches_konflux_mod_flags():
    from wrappers.go import Go

    done = MagicMock(returncode=0, stdout="", stderr="")
    with patch("wrappers.go.subprocess.run", return_value=done) as m_run:
        Go("/m", 60, vendor=True).build("./...")
        Go("/m", 60).build("github.com/open-policy-agent/opa")
    first, second = m_run.call_args_list
    assert first.args[0] == ["go", "build", "./..."]
    assert first.kwargs["env"]["GOFLAGS"].startswith("-mod=vendor")
    assert second.args[0][:4] == ["go", "build", "-o", os.devnull]
    assert second.kwargs["env"]["GOFLAGS"].startswith("-mod=readonly")


def test_update_module_reports_baseline_failure():
    fake = FakeGo(_mod(), {}, set())
    fake.build = MagicMock(
        side_effect=GoCommandError(
            "# github.com/x/y\npkg/a.go:1: undefined: libvirt.ConnectAuth"
        )
    )
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert pipeline.update_module(_module(fake), result) is False
    # The tidy/vendor probe runs first now: a committed build failure means
    # even re-vendoring doesn't help, so it's reported as a manual update.
    assert result.rejected[0].reason == (
        "re-running `go mod tidy`/`go mod vendor` with no bump breaks the "
        "build (pkg/a.go:1: undefined: libvirt.ConnectAuth); "
        "needs a manual update"
    )


# --- images and rendering -------------------------------------------------------


def test_refresh_ref_digest_pin_resolves_manifest_list_digest():
    run = MagicMock()
    run.tags_of.return_value = [
        "v4.16.0-202607311921.p2.g6f88b58.assembly.stream.el9",
        "v4.16.0-202610011108.p2.g33afbae.assembly.stream.el9",
    ]
    inspect = MagicMock(
        return_value={
            "Labels": {
                "version": "v4.16.0",
                "release": "202607311921.p2.g6f88b58.assembly.stream.el9",
            }
        }
    )
    raw = b'{"manifests": []}'
    skopeo = MagicMock(
        inspect=inspect, inspect_raw=MagicMock(return_value=raw)
    )
    with patch("pipelines.dep_bump.Skopeo", return_value=skopeo):
        new = pipeline._refresh_ref(
            run, "registry.redhat.io/openshift4/ose-cli-rhel9@sha256:old"
        )
    import hashlib

    assert new == (
        "registry.redhat.io/openshift4/ose-cli-rhel9@sha256:"
        + hashlib.sha256(raw).hexdigest()
    )
    skopeo.inspect_raw.assert_called_once_with(
        "registry.redhat.io/openshift4/ose-cli-rhel9:"
        "v4.16.0-202610011108.p2.g33afbae.assembly.stream.el9"
    )


def test_render_body_and_commit():
    result = DepBumpRepoResultDTO(
        repo="forklift",
        branch="release-2.12",
        images=[
            ImageBumpDTO(
                file="build/api/Containerfile-downstream",
                stage="builder",
                old="a:1-1",
                new="a:1-2",
            )
        ],
        go_bumps=[
            GoBumpDTO(
                module=".",
                path="golang.org/x/text",
                old="v0.36.0",
                new="v0.39.0",
                kind="security",
                ids=["GO-2026-5970", "CVE-2026-56852"],
            ),
            GoBumpDTO(
                module=".",
                path="golang.org/x/sys",
                old="v0.36.0",
                new="v0.46.0",
                kind="dependency",
            ),
        ],
        rpm_components=[".konflux/forklift-api"],
    )
    body = db.render_body(result, "scripts/rpm_lockfile_refresh.sh")
    assert (
        "| `.` | `golang.org/x/text` | v0.36.0 | v0.39.0 | security "
        "| GO-2026-5970, CVE-2026-56852 |" in body
    )
    assert (
        "scripts/rpm_lockfile_refresh.sh /path/to/checkout "
        ".konflux/forklift-api" in body
    )
    commit = db.render_commit(result, "chore(deps): title")
    assert commit.splitlines()[0] == "chore(deps): title"
    assert "- .: golang.org/x/text v0.36.0 -> v0.39.0" in commit
    assert "golang.org/x/sys" not in commit


def test_process_repo_skips_when_open_pr_exists():
    import asyncio

    args = Namespace(
        branch="release-2.12",
        apply=False,
        update_existing=False,
        skip_images=False,
        skip_go=False,
        report_dir="",
    )
    cfg = {
        "pr_title": "chore(deps): updates for {version}",
        "repos": {"forklift": {"gh_repo": "kubev2v/forklift"}},
    }
    gh = MagicMock()
    gh.list_open_prs_for_base.return_value = [
        {
            "headRefName": "dep-bump-2.12-x",
            "title": "chore(deps): updates for 2.12",
        }
    ]
    with (
        patch("pipelines.dep_bump.GHCLI", return_value=gh),
        patch("pipelines.dep_bump.Git") as m_git,
    ):
        result = asyncio.run(pipeline.process_repo("forklift", cfg, args))
    assert result.skipped and "dep-bump-2.12-x" in result.skip_reason
    m_git.assert_not_called()
