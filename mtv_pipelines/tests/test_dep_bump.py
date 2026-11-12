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


def test_newest_in_stream_jumps_go_minor_and_skips_rhel_and_source_tags():
    tags = GO_TOOLSET_TAGS + ["1.26-1785000000"]
    assert (
        db.newest_in_stream("1.25.9-1778604137", tags, 1790000000)
        == "1.26.1-1790000000"
    )


def test_newest_in_stream_stays_on_minor_until_next_one_settles():
    tags = GO_TOOLSET_TAGS + ["1.26.2-1799000000"]
    # 1.26's first build is too recent: newest 1.25.x only
    assert (
        db.newest_in_stream("1.25.9-1778604137", tags, 1789999999)
        == "1.25.10-1783614278"
    )
    # Once 1.26 has settled, its newest build counts, however recent
    assert (
        db.newest_in_stream("1.25.9-1778604137", tags, 1790000000)
        == "1.26.2-1799000000"
    )


def test_newest_in_stream_jumps_rhel_minor_within_major():
    tags = ["9.6-1760340943", "9.8-1790000000", "10.2-1792744468"]
    assert (
        db.newest_in_stream("9.6-1760340943", tags, 1790000000)
        == "9.8-1790000000"
    )


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


OCP_TAGS = [
    "v4.18.0-202610071009.p2.gbc7aeec.assembly.stream.el9",
    "v4.20.0-202609010000.p2.gaaaaaaa.assembly.stream.el9",
    "v4.20.0-202611010000.p2.gbbbbbbb.assembly.stream.el9",
    "v4.21.0-202611020000.p2.gccccccc.assembly.stream.el9",
    "v4.22.0-202611030000.p2.gddddddd.assembly.stream.el8",
]


def test_ocp_range_from_release_conf():
    conf = "RELEASE=v2.12\n# Which OCP versions\nOCP_VERSIONS=v4.20-v4.22\n"
    assert db.ocp_range(conf) == ("4.20", "4.22")
    assert db.ocp_range("RELEASE=v2.12\n") is None


def test_ocp_target_stream_moves_only_when_outside_the_range():
    # Below the range: lowest supported stream published for its elN
    assert db.ocp_target_stream("4.18", "el9", OCP_TAGS, ("4.20", "4.22")) == (
        "4.20"
    )
    # Inside the range: stays, even above the floor
    assert db.ocp_target_stream("4.21", "el9", OCP_TAGS, ("4.20", "4.22")) == (
        "4.21"
    )
    # Nothing published in range for el9 (main: v4.23-v5.1): newest below
    assert db.ocp_target_stream("4.16", "el9", OCP_TAGS, ("4.23", "5.1")) == (
        "4.21"
    )
    # Above the range floor already, or no range: stays
    assert db.ocp_target_stream("4.21", "el9", OCP_TAGS, ("4.18", "4.20")) == (
        "4.21"
    )
    assert db.ocp_target_stream("4.18", "el9", OCP_TAGS, None) == "4.18"


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
    run.cfg = {
        "cascade_guard": GUARD,
        "max_single_attempts": 20,
        "go_build_parallelism": 2,
    }
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
    module.allow_go_raise = lambda directive: allow_raise
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


def test_update_module_only_security_fixes_raise_go_directive():
    base = _mod(
        go="1.25.0",
        **{
            "golang.org__x__net": "v0.57.0",
            "github.com__gin-contrib__cors": "v1.7.5",
        },
    )
    effects = {
        "golang.org/x/net@v0.62.0": {"go": "1.26.0"},
        "github.com/gin-contrib/cors@v1.7.11": {"go": "1.26.10"},
    }
    fake = FakeGo(base, effects, set())
    fake.govulncheck = lambda: [
        {"osv": {"id": "GO-2026-6860", "aliases": []}},
        {
            "finding": {
                "osv": "GO-2026-6860",
                "fixed_version": "v0.62.0",
                "trace": [{"module": "golang.org/x/net"}],
            }
        },
    ]
    fake.list_updates = lambda: [
        {
            "Path": "github.com/gin-contrib/cors",
            "Version": "v1.7.5",
            "Update": {"Version": "v1.7.11"},
        }
    ]
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    assert (
        pipeline.update_module(_module(fake, allow_raise=True), result) is True
    )
    bumps = {(b.path, b.new) for b in result.go_bumps}
    assert ("golang.org/x/net", "v0.62.0") in bumps
    assert ("go", "1.26.0") in bumps
    [rejected] = result.rejected
    assert rejected.path == "github.com/gin-contrib/cors"
    assert "only security fixes raise the go directive" in rejected.reason


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


def test_first_line_keeps_cause_after_imports_header():
    # go mod tidy reports an "x imports" header line; the cause follows it
    err = GoCommandError(
        "go: github.com/kubev2v/forklift/cmd/hyperv-provider-server imports\n"
        "\tgithub.com/kubev2v/forklift/pkg/libvirt: no required module "
        "provides package github.com/libvirt/libvirt-go\n"
    )
    assert "no required module" in pipeline._first_line(err)
    # Plain one-line errors are unaffected
    assert pipeline._first_line(GoCommandError("boom")) == "boom"


def test_go_parallelism_caps_gomaxprocs():
    from wrappers.go import Go

    done = MagicMock(returncode=0, stdout="", stderr="")
    with patch("wrappers.go.subprocess.run", return_value=done) as m_run:
        Go("/m", 60, parallelism=2).build("./...")
        assert m_run.call_args.kwargs["env"]["GOMAXPROCS"] == "2"
        # An explicit GOMAXPROCS from the runner wins over the cap
        with patch.dict(os.environ, {"GOMAXPROCS": "4"}):
            Go("/m", 60, parallelism=2).build("./...")
        assert m_run.call_args.kwargs["env"]["GOMAXPROCS"] == "4"
        # No parallelism configured: leave the environment alone
        Go("/m", 60).build("./...")
        assert "GOMAXPROCS" not in m_run.call_args.kwargs["env"]


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
    run.ocp_range = None
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
            run,
            "registry.redhat.io/openshift4/ose-cli-rhel9@sha256:old",
            DepBumpRepoResultDTO(repo="forklift", branch="release-2.12"),
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


def test_refresh_ref_ose_tag_moves_into_supported_range():
    run = MagicMock()
    run.tags_of.return_value = OCP_TAGS
    run.ocp_range = ("4.20", "4.22")
    run.reasons = {}
    repo = "registry.redhat.io/openshift4/ose-ansible-rhel9-operator"
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    new = pipeline._refresh_ref(run, f"{repo}:{OCP_TAGS[0]}", result)
    assert new == f"{repo}:{OCP_TAGS[2]}"
    assert run.reasons[new] == (
        "OpenShift v4.18 -> v4.20 (release supports v4.20-v4.22)"
    )


def test_toolsets_ship_reads_go_from_every_go_toolset():
    run = MagicMock()
    run.repo_cfg = {"containerfiles": ["*"], "exclude": []}
    files = {
        "a": "FROM registry.redhat.io/ubi9/go-toolset:1.26.10-1 AS builder\n"
        "FROM registry.redhat.io/ubi9-minimal:9.8-1\n",
        "b": "FROM registry.redhat.io/ubi9/go-toolset:9.8-2 AS builder\n",
    }
    run.read = files.__getitem__
    run.go_versions = {
        "registry.redhat.io/ubi9/go-toolset:1.26.10-1": "1.26.10",
        "registry.redhat.io/ubi9/go-toolset:9.8-2": "1.26.10",
    }
    with patch("pipelines.dep_bump.containerfiles", return_value=["a", "b"]):
        assert pipeline._toolsets_ship(run, "1.26.0")
        assert pipeline._toolsets_ship(run, "1.26.10")
        assert not pipeline._toolsets_ship(run, "1.27.0")


def test_toolsets_ship_label_lookup_failure_is_not_fatal():
    run = MagicMock()
    run.repo_cfg = {"containerfiles": ["*"], "exclude": []}
    run.read = lambda rel: (
        "FROM registry.redhat.io/ubi9/go-toolset:1.26.10-1 AS builder\n"
    )
    run.go_versions = {}
    skopeo = MagicMock()
    skopeo.inspect.side_effect = RuntimeError("unauthorized")
    with (
        patch("pipelines.dep_bump.containerfiles", return_value=["a"]),
        patch("pipelines.dep_bump.Skopeo", return_value=skopeo),
    ):
        assert not pipeline._toolsets_ship(run, "1.26.0")


def test_attempt_builder_go_raise_needs_a_build_with_that_go():
    base = _mod(go="1.25.0", **{"golang.org__x__net": "v0.57.0"})
    effects = {"golang.org/x/net@v0.62.0": {"go": "1.26.0"}}
    fake = FakeGo(base, effects, set())
    module = _module(fake, allow_raise=True)
    builder = "registry.redhat.io/ubi9/go-toolset:1.26.10-1"
    module.builder_bumps = lambda directive: [
        ("Containerfile", 0, "builder", "old", builder)
    ]
    # The image step already found the modules don't build with 1.26.10
    module.run.toolchains = {"1.26.10": "`.` doesn't build with go 1.26.10"}
    cand = db.Candidate("golang.org/x/net", "v0.62.0", "security")
    ok, reason, _ = module.attempt(base, [cand])
    assert not ok
    assert reason == "`.` doesn't build with go 1.26.10"


def _toolset_run(tags):
    run = MagicMock()
    run.tags_of.return_value = tags
    run.settled_before = 1790000000
    run.toolchains = {}
    run.repo_dir = "/repo"
    run.repo_cfg = {
        "go_modules": [{"dirs": ["."], "vendor": True, "build": "./..."}]
    }
    return run


def test_refresh_ref_go_minor_jump_needs_a_build_with_that_go():
    run = _toolset_run(
        ["1.25.9-1778604137", "1.25.10-1783614278", "1.26.1-1790000000"]
    )
    repo = "registry.redhat.io/ubi9/go-toolset"
    run.go_versions = {
        f"{repo}:1.25.9-1778604137": "1.25.9",
        f"{repo}:1.25.10-1783614278": "1.25.10",
        f"{repo}:1.26.1-1790000000": "1.26.1",
    }
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    ref = f"{repo}:1.25.9-1778604137"
    go = MagicMock()
    with (
        patch("pipelines.dep_bump.os.path.exists", return_value=True),
        patch("pipelines.dep_bump.Go", return_value=go) as go_cls,
    ):
        assert pipeline._refresh_ref(run, ref, result).endswith(
            ":1.26.1-1790000000"
        )
        assert go_cls.call_args.kwargs["toolchain"] == "go1.26.1"

        # The same Go fails for another run: fall back to the newest 1.25.x
        run.toolchains = {}
        go.build.side_effect = GoCommandError("x.go:1: some breakage")
        assert pipeline._refresh_ref(run, ref, result).endswith(
            ":1.25.10-1783614278"
        )
    assert result.rejected[0].target == "go 1.26.1"
    assert "doesn't build with go 1.26.1" in result.rejected[0].reason


def test_refresh_ref_rhel_versioned_toolset_checked_by_label_go():
    run = _toolset_run(["9.8-1780000000", "9.9-1785000000"])
    repo = "registry.redhat.io/ubi9/go-toolset"
    run.go_versions = {
        f"{repo}:9.8-1780000000": "1.26.10",
        f"{repo}:9.9-1785000000": "1.27.3",
    }
    result = DepBumpRepoResultDTO(repo="forklift", branch="main")
    go = MagicMock()
    go.build.side_effect = GoCommandError("x.go:1: some breakage")
    with (
        patch("pipelines.dep_bump.os.path.exists", return_value=True),
        patch("pipelines.dep_bump.Go", return_value=go) as go_cls,
    ):
        new = pipeline._refresh_ref(run, f"{repo}:9.8-1780000000", result)
    assert new is None
    assert go_cls.call_args.kwargs["toolchain"] == "go1.27.3"
    assert result.rejected[0].target == "go 1.27.3"


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


# --- Jira ticket matching -------------------------------------------------------


def _issue(key, summary, labels):
    return {"key": key, "fields": {"summary": summary, "labels": labels}}


def test_cve_search_jql_sorts_and_quotes_labels():
    assert db.cve_search_jql(["CVE-2026-56852", "CVE-2026-56851"]) == (
        "project = MTV AND issuetype = Vulnerability "
        "AND labels = SecurityTracking AND statusCategory != Done "
        'AND labels IN ("CVE-2026-56851", "CVE-2026-56852")'
    )


def test_ticket_cves_keeps_only_this_branch_stream():
    issues = [
        _issue("MTV-6382", "CVE-2026-56852 ... [mtv-2.12]", ["CVE-2026-56852"]),
        _issue("MTV-6383", "CVE-2026-56852 ... [mtv-2.11]", ["CVE-2026-56852"]),
        _issue("MTV-6384", "CVE-2026-56852 (no stream)", ["CVE-2026-56852"]),
    ]
    assert db.ticket_cves(issues, "release-2.12") == {
        "CVE-2026-56852": ["MTV-6382"]
    }


def test_base_ref_from_comments_requires_base_bump_marker():
    comments = [
        "a human comment",
        "[cve-triage:AFFECTED]\nBase image: registry.redhat.io/x",
        "[cve-triage:BASE_BUMP_NEEDED]\nstuff\n"
        "Base image: registry.redhat.io/openshift4/ose-cli-rhel9@sha256:ab "
        "(OpenShift 4.16)",
    ]
    assert (
        db.base_ref_from_comments(comments)
        == "registry.redhat.io/openshift4/ose-cli-rhel9@sha256:ab"
    )
    assert db.base_ref_from_comments(comments[:2]) == ""


class _FakeJira:
    """Answers the two searches; comments are per ticket key."""

    def __init__(self, cve_issues=(), base_issues=(), comments=None):
        self.cve_issues = list(cve_issues)
        self.base_issues = list(base_issues)
        self.comments = comments or {}

    def search(self, jql, fields):
        return self.base_issues if "triage:needs-fix" in jql else self.cve_issues

    def get_comments(self, key):
        return self.comments.get(key, [])


def _result_with_bumps():
    result = DepBumpRepoResultDTO(repo="forklift", branch="release-2.12")
    result.go_bumps.append(
        GoBumpDTO(
            module=".",
            path="golang.org/x/text",
            old="v0.36.0",
            new="v0.41.0",
            kind="security",
            ids=["GO-2026-5970", "CVE-2026-56852"],
        )
    )
    result.go_bumps.append(
        GoBumpDTO(module=".", path="github.com/other", old="v1.0.0", new="v1.0.1", kind="patch")
    )
    result.images.append(
        ImageBumpDTO(
            file="build/Dockerfile-downstream",
            old="registry.redhat.io/openshift4/ose-cli-rhel9@sha256:old",
            new="registry.redhat.io/openshift4/ose-cli-rhel9@sha256:new",
        )
    )
    return result


def test_link_jira_matches_cves_and_base_images():
    result = _result_with_bumps()
    jira = _FakeJira(
        cve_issues=[_issue("MTV-6382", "x [mtv-2.12]", ["CVE-2026-56852"])],
        base_issues=[_issue("MTV-6790", "y [mtv-2.12]", ["triage:needs-fix"])],
        comments={
            "MTV-6790": [
                "[cve-triage:BASE_BUMP_NEEDED]\nBase image: "
                "registry.redhat.io/openshift4/ose-cli-rhel9@sha256:old"
            ]
        },
    )
    db.link_jira(result, jira)
    assert result.go_bumps[0].jira_keys == ["MTV-6382"]
    assert result.go_bumps[1].jira_keys == []
    assert result.images[0].jira_keys == ["MTV-6790"]


def test_link_jira_skips_other_branch_stream_and_other_repo():
    result = _result_with_bumps()
    jira = _FakeJira(
        cve_issues=[_issue("MTV-6383", "x [mtv-2.11]", ["CVE-2026-56852"])],
        base_issues=[_issue("MTV-6790", "y [mtv-2.12]", ["triage:needs-fix"])],
        comments={
            "MTV-6790": [
                "[cve-triage:BASE_BUMP_NEEDED]\nBase image: "
                "registry.redhat.io/ubi9-minimal:9.7-1"
            ]
        },
    )
    db.link_jira(result, jira)
    assert result.go_bumps[0].jira_keys == []
    assert result.images[0].jira_keys == []


def test_render_body_and_commit_carry_resolves_lines():
    result = _result_with_bumps()
    result.go_bumps[0].jira_keys = ["MTV-6382"]
    result.images[0].jira_keys = ["MTV-6790"]
    body = db.render_body(result, "scripts/rpm_lockfile_refresh.sh")
    assert "## Jira tickets" in body
    assert (
        "| MTV-6382 | `golang.org/x/text` v0.36.0 -> v0.41.0 (security) |"
        in body
    )
    assert (
        "| MTV-6790 | base image "
        "`registry.redhat.io/openshift4/ose-cli-rhel9@sha256:old` "
        "(build/Dockerfile-downstream) |" in body
    )
    assert "Resolves: MTV-6382" in body and "Resolves: MTV-6790" in body
    commit = db.render_commit(result, "chore(deps): title")
    assert "Resolves: MTV-6382" in commit and "Resolves: MTV-6790" in commit
