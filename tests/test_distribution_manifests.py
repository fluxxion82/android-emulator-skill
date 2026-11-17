"""Each agent's marketplace file leads to this skill, read from the files alone.

Two agents install this repository as a plugin, and each starts from its own
file: Claude Code from `.claude-plugin/marketplace.json`, Codex from
`.agents/plugins/marketplace.json`. Each names a plugin directory; the plugin
directory holds one manifest per agent (`.claude-plugin/plugin.json`,
`.codex-plugin/plugin.json`); the manifest names, or implies, a skills
directory; and the skill is a directory under that. An install copies the
plugin directory and nothing else of the repository, so every step of that
chain has to stay inside it. Nothing checked the chain: the nearest guard
resolved the clone-and-link fallback of the README.

So the chain is walked here as an installer walks it, on the files, with no
agent run: both marketplace files parse and list this one plugin, each source
resolves to a directory of this repository, each manifest is there and names
the same plugin, its skills directory resolves, and `SKILL.md`, `scripts/` and
`references/` are in the skill it leads to -- the same skill for both.

A place in a manifest is its keys and list indices, kept as a tuple: two
places are never told apart by how they would be written. A path a manifest
writes is held to a rule about where strings appear, not
about which keys are paths: **any string value that begins `./`, `../`, `/`
or `~` begins `./`, holds no `..`, resolves inside the plugin directory (a
link that points out of it does not) and exists.** The
keys Codex requires to be paths (`skills`, `interface.composerIcon`,
`interface.logo`) must be present and written that way. What that does not
see, stated: a path written with no leading `./` under a key this module does
not name is read as text.

Not checked here, because a file cannot show it: that either agent accepts
these manifests. Installing into an empty home with each agent's own command
is `tests/verify_install.py`'s job.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from test_packaging_contract import PACKAGE_ROOT, REPO_ROOT, SKILL_ROOT

PLUGIN = "android-emulator-skill"
SKILL = "android-emulator-skill"
LISTING = "agents/openai.yaml"
# What an installed agent needs of the skill: the file it loads, and the two
# directories that file's commands and links point into.
SHIPPED = (("SKILL.md", Path.is_file), ("scripts", Path.is_dir), ("references", Path.is_dir))


@dataclass(frozen=True)
class Surface:
    """One agent's way in: its marketplace file, and its manifest inside the plugin."""

    agent: str
    marketplace: str
    manifest: str
    # Where the agent looks for skills when the manifest names no directory;
    # None when the manifest has to name one.
    skills_default: str | None
    required_paths: tuple[str, ...]


CLAUDE = Surface(
    "Claude Code", ".claude-plugin/marketplace.json", ".claude-plugin/plugin.json", "./skills/", ()
)
CODEX = Surface(
    "Codex",
    ".agents/plugins/marketplace.json",
    ".codex-plugin/plugin.json",
    None,
    ("skills", "interface.composerIcon", "interface.logo"),
)
SURFACES = (CLAUDE, CODEX)

_WRITTEN_AS_A_PATH = re.compile(r"\.{1,2}/|/|~")


def _read_json(path: Path) -> tuple[object, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as error:
        return None, f"cannot be read as JSON ({type(error).__name__})"


def _inside(root: Path, written: object) -> tuple[Path | None, str | None]:
    """The file or directory a manifest's path names, or why it names none an install would hold."""
    if not isinstance(written, str) or not written.startswith("./"):
        return None, f"{written!r} is not a path written from `./`"
    if ".." in written.split("/"):
        return None, f"{written!r} climbs with `..`"
    resolved = (root / written).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None, f"{written!r} leaves {root.name}/"
    if not resolved.exists():
        return None, f"{written!r} is not there"
    return resolved, None


Place = tuple[str | int, ...]


def _strings(value: object, at: Place = ()) -> Iterator[tuple[Place, str]]:
    """Every string in a parsed document, with its place: the keys and list indices down to it.

    A place is a tuple, never a joined string: `("defaultPrompt", 0)` and the
    one key `("defaultPrompt.0",)` are different places that would be written
    the same way. `_label` joins one for a message, and only for a message.
    """
    if isinstance(value, str):
        yield at, value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield from _strings(inner, (*at, str(key)))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _strings(inner, (*at, index))


def _keys(value: object, at: Place = ()) -> Iterator[Place]:
    """The place of every mapping key in a parsed document, whatever its value is."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield (*at, str(key))
            yield from _keys(inner, (*at, str(key)))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _keys(inner, (*at, index))


def _label(place: Place) -> str:
    return ".".join(str(part) for part in place)


def _at(document: object, dotted: str) -> object:
    for key in dotted.split("."):
        document = document.get(key) if isinstance(document, dict) else None
    return document


def _path_problems(document: object, root: Path) -> list[str]:
    """Every string written as a path resolves inside `root`."""
    problems = []
    for at, written in _strings(document):
        if _WRITTEN_AS_A_PATH.match(written):
            _, problem = _inside(root, written)
            if problem:
                problems.append(f"`{_label(at)}`: {problem}")
    return problems


def _source(entry: dict) -> object:
    """What a marketplace entry gives as the plugin's place in this repository.

    Claude Code's file writes a relative path; Codex's writes
    `{"source": "local", "path": ...}`. Any other source (a URL, another
    repository, a registry) is handed back as it stands and is not a path:
    an install would then fetch something this repository does not hold.
    """
    source = entry.get("source")
    if isinstance(source, dict) and source.get("source") == "local":
        return source.get("path")
    return source


def _plugin_entry(root: Path, surface: Surface) -> tuple[dict | None, str | None]:
    market, problem = _read_json(root / surface.marketplace)
    if problem:
        return None, problem
    plugins = market.get("plugins") if isinstance(market, dict) else None
    if not isinstance(plugins, list) or len(plugins) != 1 or not isinstance(plugins[0], dict):
        return None, f"lists something other than the one plugin {PLUGIN}"
    if plugins[0].get("name") != PLUGIN:
        return None, f"lists {plugins[0].get('name')!r}, not {PLUGIN}"
    return plugins[0], None


def _manifest(root: Path, surface: Surface) -> tuple[Path | None, dict | None, str | None]:
    """The plugin directory a surface's marketplace names, and that surface's manifest in it."""
    entry, problem = _plugin_entry(root, surface)
    if problem:
        return None, None, f"{surface.marketplace}: {problem}"
    plugin_root, problem = _inside(root, _source(entry))
    if problem or not plugin_root.is_dir():
        return None, None, f"{surface.marketplace}: source {problem or 'is not a directory'}"
    manifest, problem = _read_json(plugin_root / surface.manifest)
    if problem or not isinstance(manifest, dict):
        return plugin_root, None, f"{surface.manifest}: {problem or 'is not an object'}"
    return plugin_root, manifest, None


def distribution_problems(root: Path) -> list[str]:
    """Where a marketplace file of the repository at `root` fails to lead to this skill."""
    problems = []
    skills_reached = {}
    for surface in SURFACES:
        plugin_root, manifest, problem = _manifest(root, surface)
        if problem:
            problems.append(problem)
            continue
        where = surface.manifest
        if manifest.get("name") != PLUGIN:
            problems.append(f"{where}: names {manifest.get('name')!r}, not {PLUGIN}")
        problems += [f"{where}: {problem}" for problem in _path_problems(manifest, plugin_root)]
        for dotted in surface.required_paths:
            written = _at(manifest, dotted)
            if not isinstance(written, str) or not written.startswith("./"):
                problems.append(f"{where}: `{dotted}` is required and names no path from `./`")
        skills, problem = _inside(plugin_root, manifest.get("skills", surface.skills_default))
        if problem or not skills.is_dir():
            problems.append(f"{where}: skills directory {problem or 'is not a directory'}")
            continue
        skill = skills / SKILL
        for name, is_there in SHIPPED:
            if not is_there(skill / name):
                problems.append(
                    f"{where}: leads to {skill.relative_to(root.resolve())}, "
                    f"which holds no {name}"
                )
        skills_reached[surface.agent] = skill
    if len(set(skills_reached.values())) > 1:
        problems.append(f"the agents are led to different skills: {skills_reached}")
    return problems


def _listing(root: Path) -> tuple[Path, object, str | None]:
    path = root / PLUGIN / "skills" / SKILL / LISTING
    try:
        return path, yaml.safe_load(path.read_text(encoding="utf-8")), None
    except (OSError, yaml.YAMLError) as error:
        return path, None, f"{LISTING}: cannot be read as YAML ({type(error).__name__})"


def listing_problems(root: Path) -> list[str]:
    """`agents/openai.yaml` is how Codex lists the skill; it says what the manifest says.

    Two of its three lines are copies of the Codex manifest's `interface`
    (the name and the short description), so they are compared rather than
    trusted to be edited together. A path it writes is relative to the skill
    directory, where the file's own `agents/` sits.
    """
    path, listing, problem = _listing(root)
    if problem:
        return [problem]
    interface = listing.get("interface") if isinstance(listing, dict) else None
    if not isinstance(interface, dict):
        return [f"{LISTING}: has no `interface` mapping"]
    problems = [
        f"{LISTING}: `{key}` is not a line of text"
        for key in ("display_name", "short_description", "default_prompt")
        if not isinstance(interface.get(key), str) or not interface[key].strip()
    ]
    problems += [f"{LISTING}: {problem}" for problem in _path_problems(listing, path.parents[1])]
    _, manifest, problem = _manifest(root, CODEX)
    if problem:
        return [*problems, problem]
    for mine, theirs in (
        ("display_name", "displayName"),
        ("short_description", "shortDescription"),
    ):
        if interface.get(mine) != _at(manifest, f"interface.{theirs}"):
            problems.append(
                f"{LISTING}: `{mine}` is {interface.get(mine)!r} and {CODEX.manifest} says "
                f"{_at(manifest, f'interface.{theirs}')!r}"
            )
    return problems


# Under an `interface`, every string is read as a description unless its key
# is one of these: names, links, colours and files. By key and never by what
# the value looks like -- a prompt that is a link is still a prompt -- and the
# default is to read, so a key nobody listed is held to the rule.
NOT_DESCRIPTIONS = frozenset(
    {
        *("displayName", "developerName", "category", "display_name"),
        *("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"),
        *("brandColor", "brandColorDark"),
        *("composerIcon", "composerIconDark", "logo", "logoDark", "screenshots"),
        *("icon_small", "icon_large"),
    }
)


def _shown(interface: object) -> Iterator[tuple[Place, str]]:
    for place, text in _strings(interface):
        if place[0] not in NOT_DESCRIPTIONS:
            yield place, text


def plugin_descriptions(root: Path) -> list[tuple[str, object]]:
    """The plugin's own description, in each of the three places it is written."""
    entry, _ = _plugin_entry(root, CLAUDE)
    found = [(f"{CLAUDE.marketplace} plugin description", _at(entry, "description"))]
    for surface in SURFACES:
        _, manifest, _ = _manifest(root, surface)
        found.append((f"{surface.manifest} description", _at(manifest, "description")))
    return found


def descriptions(root: Path) -> list[tuple[str, object]]:
    """Every description an install surface shows, with where it is written.

    A list, in reading order: two places can share a label and neither
    replaces the other.
    """
    market, _ = _read_json(root / CLAUDE.marketplace)
    found = [(f"{CLAUDE.marketplace} description", _at(market, "description"))]
    found += plugin_descriptions(root)
    for surface in SURFACES:
        _, manifest, _ = _manifest(root, surface)
        found += [
            (f"{surface.manifest} interface.{_label(place)}", text)
            for place, text in _shown(_at(manifest, "interface"))
        ]
    _, listing, _ = _listing(root)
    found += [
        (f"{LISTING} {_label(place)}", text) for place, text in _shown(_at(listing, "interface"))
    ]
    return found


def _python_floor() -> str:
    """`Python 3.12+`, from the one place the floor is stated to a tool."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = project["project"]["requires-python"]
    assert re.fullmatch(r">=\d+\.\d+", requires), requires
    return f"Python {requires.removeprefix('>=')}+"


def description_problems(root: Path) -> list[str]:
    """No description counts anything, and the plugin's description is one sentence in three places.

    The descriptions said how many scripts there were, in four files a new
    script did not touch. The rule is about digits, not about the word
    "scripts": none stands in any description, except the Python floor
    written exactly as pyproject.toml gives it.
    """
    problems = []
    for where, text in descriptions(root):
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{where}: no description")
        elif re.search(r"\d", text.replace(_python_floor(), "")):
            problems.append(
                f"{where}: holds a number ({text!r}); a description counts nothing, and a "
                "key that is not a description is named in NOT_DESCRIPTIONS"
            )
    plugin = plugin_descriptions(root)
    if any(text != plugin[0][1] for _, text in plugin):
        places = ", ".join(where for where, _ in plugin)
        problems.append(f"the plugin is described differently in {places}")
    return problems


def version_problems(root: Path) -> list[str]:
    """Codex's marketplace entry states no version: the Codex manifest is the one place.

    Five files state the release's version and each is compared with the tag.
    A sixth, written here, would be one nothing compares.
    """
    entry, problem = _plugin_entry(root, CODEX)
    if problem:
        return [f"{CODEX.marketplace}: {problem}"]
    return [
        f"{CODEX.marketplace}: `{_label(at)}` states a version nothing checks"
        for at in _keys(entry)
        if str(at[-1]).lower() == "version"
    ]


def all_problems(root: Path) -> list[str]:
    return [
        *distribution_problems(root),
        *listing_problems(root),
        *description_problems(root),
        *version_problems(root),
    ]


def test_each_marketplace_file_leads_to_this_skill():
    assert not distribution_problems(REPO_ROOT)


def test_the_walk_ends_at_the_skill_the_suite_tests():
    """Anti-vacuity: both surfaces are walked to the end, and the end is `SKILL_ROOT`."""
    for surface in SURFACES:
        plugin_root, manifest, problem = _manifest(REPO_ROOT, surface)
        assert not problem, problem
        assert plugin_root == PACKAGE_ROOT.resolve()
        skills, problem = _inside(plugin_root, manifest.get("skills", surface.skills_default))
        assert not problem, problem
        assert skills / SKILL == SKILL_ROOT.resolve()


def test_every_asset_a_manifest_names_ships_and_one_is_named():
    """Anti-vacuity: the reader of written paths finds the three Codex requires, icons included."""
    _, manifest, _ = _manifest(REPO_ROOT, CODEX)
    written = [_label(at) for at, text in _strings(manifest) if _WRITTEN_AS_A_PATH.match(text)]

    assert set(CODEX.required_paths) <= set(written), written
    for dotted in ("interface.composerIcon", "interface.logo"):
        icon, problem = _inside(PACKAGE_ROOT, _at(manifest, dotted))
        assert not problem, problem
        assert icon.is_file() and icon.stat().st_size, f"`{dotted}` names an empty file"


def test_the_skills_listing_for_codex_says_what_the_manifest_says():
    assert not listing_problems(REPO_ROOT)


def test_no_description_counts_scripts_and_the_plugin_has_one_description():
    assert not description_problems(REPO_ROOT)

    read = {where for where, _ in descriptions(REPO_ROOT)}
    interface = f"{CODEX.manifest} interface"
    assert {
        f"{CLAUDE.marketplace} description",
        *(where for where, _ in plugin_descriptions(REPO_ROOT)),
        f"{interface}.shortDescription",
        f"{interface}.longDescription",
        f"{interface}.capabilities.0",
        f"{interface}.defaultPrompt.0",
        f"{interface}.defaultPrompt.1",
        f"{LISTING} short_description",
        f"{LISTING} default_prompt",
    } <= read, sorted(read)
    assert not {where for where in read if "URL" in where or "Color" in where or "Icon" in where}


def test_the_codex_marketplace_entry_states_no_version():
    assert not version_problems(REPO_ROOT)


def _miniature(tmp_path: Path) -> Path:
    """The files the walk reads, copied: both marketplaces, the plugin's manifests, a bare skill."""
    root = tmp_path / "repository"
    for relative in (CLAUDE.marketplace, CODEX.marketplace):
        (root / relative).parent.mkdir(parents=True)
        shutil.copy(REPO_ROOT / relative, root / relative)
    plugin = root / PLUGIN
    for relative in (CLAUDE.manifest, CODEX.manifest):
        (plugin / relative).parent.mkdir(parents=True)
        shutil.copy(PACKAGE_ROOT / relative, plugin / relative)
    shutil.copytree(PACKAGE_ROOT / "assets", plugin / "assets")
    skill = plugin / "skills" / SKILL
    (skill / "agents").mkdir(parents=True)
    shutil.copy(SKILL_ROOT / LISTING, skill / LISTING)
    shutil.copy(SKILL_ROOT / "SKILL.md", skill / "SKILL.md")
    (skill / "scripts").mkdir()
    (skill / "references").mkdir()
    return root


def _edit(relative: str, change: Callable[[dict], object]) -> Callable[[Path], None]:
    """A mutation: one JSON file of the miniature, parsed, changed and written back."""

    def apply(root: Path) -> None:
        path = root / relative
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document)
        path.write_text(json.dumps(document), encoding="utf-8")

    return apply


def _set(dotted: str, value: object) -> Callable[[dict], object]:
    def change(document: dict) -> None:
        *parents, last = dotted.split(".")
        for key in parents:
            document = document[int(key)] if isinstance(document, list) else document[key]
        document[last] = value

    return change


def _drop(dotted: str) -> Callable[[dict], object]:
    def change(document: dict) -> None:
        *parents, last = dotted.split(".")
        for key in parents:
            document = document[int(key)] if isinstance(document, list) else document[key]
        del document[last]

    return change


def _link_out(inside: Path, outside: Path) -> None:
    """Replace a file of the plugin with a link to a copy of it kept outside the plugin."""
    shutil.move(inside, outside)
    inside.symlink_to(outside)


CODEX_MANIFEST = f"{PLUGIN}/{CODEX.manifest}"
CLAUDE_MANIFEST = f"{PLUGIN}/{CLAUDE.manifest}"
ELSEWHERE = {"source": "url", "url": "https://example.invalid/plugin.git"}

MUTATIONS = {
    "the Codex marketplace file is gone": (
        lambda root: (root / CODEX.marketplace).unlink(),
        ".agents/plugins/marketplace.json: cannot be read",
    ),
    "a marketplace lists a second plugin": (
        _edit(CLAUDE.marketplace, lambda market: market["plugins"].append({"name": "other"})),
        ".claude-plugin/marketplace.json: lists something other than the one plugin",
    ),
    "a marketplace lists another plugin": (
        _edit(CODEX.marketplace, _set("plugins.0.name", "ios-simulator-skill")),
        "lists 'ios-simulator-skill'",
    ),
    "the Codex source is a directory that is not there": (
        _edit(CODEX.marketplace, _set("plugins.0.source.path", "./plugins/android")),
        "source './plugins/android' is not there",
    ),
    "the Codex source climbs out of the repository": (
        _edit(CODEX.marketplace, _set("plugins.0.source.path", f"../repository/{PLUGIN}")),
        "is not a path written from `./`",
    ),
    "the Codex source leaves by the back": (
        _edit(CODEX.marketplace, _set("plugins.0.source.path", f"./../repository/{PLUGIN}")),
        "climbs with `..`",
    ),
    "the Codex source is fetched from elsewhere": (
        _edit(CODEX.marketplace, _set("plugins.0.source", ELSEWHERE)),
        "is not a path written from `./`",
    ),
    "the Claude source is the skills directory": (
        _edit(CLAUDE.marketplace, _set("plugins.0.source", f"./{PLUGIN}/skills")),
        ".claude-plugin/plugin.json: cannot be read",
    ),
    "the Codex manifest names another plugin": (
        _edit(CODEX_MANIFEST, _set("name", "android")),
        ".codex-plugin/plugin.json: names 'android'",
    ),
    "the Codex manifest names no skills directory": (
        _edit(CODEX_MANIFEST, _drop("skills")),
        "`skills` is required",
    ),
    "the Codex manifest's skills directory holds no skill": (
        _edit(CODEX_MANIFEST, _set("skills", "./assets/")),
        "which holds no SKILL.md",
    ),
    "the Claude manifest names a skills directory that is not there": (
        _edit(CLAUDE_MANIFEST, _set("skills", "./skill/")),
        ".claude-plugin/plugin.json: `skills`: './skill/' is not there",
    ),
    "the skill has no references": (
        lambda root: (root / PLUGIN / "skills" / SKILL / "references").rmdir(),
        "which holds no references",
    ),
    "the logo is a file that does not ship": (
        _edit(CODEX_MANIFEST, _set("interface.logo", "./assets/logo.png")),
        "`interface.logo`: './assets/logo.png' is not there",
    ),
    "the composer icon is the site's": (
        _edit(CODEX_MANIFEST, _set("interface.composerIcon", "../site/favicon.svg")),
        "`interface.composerIcon`: '../site/favicon.svg' is not a path written from `./`",
    ),
    "the composer icon leaves the plugin by the back": (
        _edit(CODEX_MANIFEST, _set("interface.composerIcon", "./assets/../../site/favicon.svg")),
        "`interface.composerIcon`: './assets/../../site/favicon.svg' climbs with `..`",
    ),
    "the icon is a link to a file outside the plugin": (
        lambda root: _link_out(root / PLUGIN / "assets" / "icon.svg", root / "favicon.svg"),
        "'./assets/icon.svg' leaves android-emulator-skill/",
    ),
    "the logo is written with no `./`": (
        _edit(CODEX_MANIFEST, _set("interface.logo", "assets/icon.svg")),
        "`interface.logo` is required and names no path from `./`",
    ),
    "there is no logo": (
        _edit(CODEX_MANIFEST, _drop("interface.logo")),
        "`interface.logo` is required",
    ),
    "a key this module does not name holds a path that is not there": (
        _edit(CODEX_MANIFEST, _set("interface.screenshots", ["./assets/one.png"])),
        "`interface.screenshots.0`: './assets/one.png' is not there",
    ),
    "a hook is an absolute path": (
        _edit(CLAUDE_MANIFEST, _set("hooks", "/etc/hooks.json")),
        "`hooks`: '/etc/hooks.json' is not a path written from `./`",
    ),
    "a description counts the scripts": (
        _edit(CODEX_MANIFEST, _set("interface.shortDescription", "33 scripts for Android")),
        "holds a number",
    ),
    "a default prompt counts": (
        _edit(CODEX_MANIFEST, _set("interface.defaultPrompt", ["Run all 33 scripts."])),
        "holds a number",
    ),
    "one of two prompts that open alike counts": (
        _edit(
            CODEX_MANIFEST,
            _set(
                "interface.defaultPrompt",
                [
                    "Use the Android emulator skill to run 33 scripts.",
                    "Use the Android emulator skill to navigate my app.",
                ],
            ),
        ),
        "interface.defaultPrompt.0: holds a number",
    ),
    "a key spelled like a place in the list sits beside the list": (
        _edit(
            CODEX_MANIFEST,
            lambda manifest: manifest["interface"].update(
                {
                    "defaultPrompt": ["Use the Android emulator skill to run 33 scripts."],
                    "defaultPrompt.0": "Use the Android emulator skill to build my app.",
                }
            ),
        ),
        "interface.defaultPrompt.0: holds a number",
    ),
    "a prompt is a link that counts": (
        _edit(
            CODEX_MANIFEST,
            _set("interface.defaultPrompt", ["https://example.com/33-scripts"]),
        ),
        "interface.defaultPrompt.0: holds a number",
    ),
    "a capability is written like a path and counts": (
        _edit(CODEX_MANIFEST, _set("interface.capabilities", ["./assets/icon.svg 33 times"])),
        "interface.capabilities.0: holds a number",
    ),
    "a key nobody listed counts": (
        _edit(CODEX_MANIFEST, _set("interface.tagline", "33 scripts for Android")),
        "interface.tagline: holds a number",
    ),
    "the Python floor is another one": (
        _edit(CODEX_MANIFEST, _set("interface.longDescription", "Requires Python 3.9+.")),
        "holds a number",
    ),
    "one copy of the plugin's description is edited alone": (
        _edit(CLAUDE_MANIFEST, _set("description", "Scripts for Android.")),
        "the plugin is described differently",
    ),
    "the marketplace has no description": (
        _edit(CLAUDE.marketplace, _drop("description")),
        ".claude-plugin/marketplace.json description: no description",
    ),
    "the Codex marketplace entry states a version": (
        _edit(CODEX.marketplace, _set("plugins.0.version", "0.9.0")),
        "`version` states a version nothing checks",
    ),
    "the Codex source states a version": (
        _edit(CODEX.marketplace, _set("plugins.0.source.version", "0.9.0")),
        "`source.version` states a version nothing checks",
    ),
    "the Codex marketplace entry states a version as a number": (
        _edit(CODEX.marketplace, _set("plugins.0.version", 0.9)),
        "`version` states a version nothing checks",
    ),
    "the Codex marketplace entry states a version in parts": (
        _edit(CODEX.marketplace, _set("plugins.0.Version", {"major": 0, "minor": 9})),
        "`Version` states a version nothing checks",
    ),
}


def test_the_miniature_is_read_as_the_repository_is(tmp_path):
    """The control for every mutation below: unchanged, the copy has nothing wrong with it."""
    assert not all_problems(_miniature(tmp_path))


@pytest.mark.parametrize("name", MUTATIONS)
def test_a_broken_link_in_the_chain_is_reported(name, tmp_path):
    mutate, reported = MUTATIONS[name]
    root = _miniature(tmp_path)
    mutate(root)

    problems = all_problems(root)
    assert any(reported in problem for problem in problems), problems


def _rewrite_listing(change: Callable[[dict], object]) -> Callable[[Path], None]:
    def apply(root: Path) -> None:
        path = root / PLUGIN / "skills" / SKILL / LISTING
        listing = yaml.safe_load(path.read_text(encoding="utf-8"))
        change(listing)
        path.write_text(yaml.safe_dump(listing), encoding="utf-8")

    return apply


LISTING_MUTATIONS = {
    "the listing is renamed alone": (
        _rewrite_listing(_set("interface.display_name", "Android")),
        "`display_name` is 'Android' and .codex-plugin/plugin.json says 'Android Emulator'",
    ),
    "the manifest's short description is edited alone": (
        _edit(CODEX_MANIFEST, _set("interface.shortDescription", "Drive Android apps")),
        "`short_description` is 'Build and drive Android apps'",
    ),
    "the listing has no default prompt": (
        _rewrite_listing(_drop("interface.default_prompt")),
        "`default_prompt` is not a line of text",
    ),
    "the listing names an icon that is not in the skill": (
        _rewrite_listing(_set("interface.icon_small", "./assets/icon.svg")),
        "`interface.icon_small`: './assets/icon.svg' is not there",
    ),
    "the listing counts": (
        _rewrite_listing(_set("interface.default_prompt", "Use one of the 33 scripts.")),
        "agents/openai.yaml default_prompt: holds a number",
    ),
    "the listing is not a mapping": (
        lambda root: (root / PLUGIN / "skills" / SKILL / LISTING).write_text("- interface\n"),
        "has no `interface` mapping",
    ),
    "the listing is gone": (
        lambda root: (root / PLUGIN / "skills" / SKILL / LISTING).unlink(),
        "agents/openai.yaml: cannot be read as YAML",
    ),
}


@pytest.mark.parametrize("name", LISTING_MUTATIONS)
def test_a_listing_that_disagrees_with_the_manifest_is_reported(name, tmp_path):
    mutate, reported = LISTING_MUTATIONS[name]
    root = _miniature(tmp_path)
    mutate(root)

    problems = all_problems(root)
    assert any(reported in problem for problem in problems), problems
