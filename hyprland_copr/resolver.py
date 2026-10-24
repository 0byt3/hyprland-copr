"""Source-build hypr dependencies; prefer Fedora for other dependencies."""

from collections import defaultdict
from pathlib import Path

from .dependencies import discover
from .model import (
    COMPANIONS,
    PipelineError,
    ROOT_COMPONENTS,
    SOURCE_POLICY,
    hypr_project,
    normalize_requirement,
    topological_order,
)


class Resolver:
    def __init__(self, sources, providers, max_candidates=10):
        self.sources = sources
        self.providers = providers
        self.max_candidates = max_candidates

    def resolve(self, candidate, *, lua_only=False, root="hyprland", include_companions=False):
        root = "lua" if lua_only else root
        if root not in ROOT_COMPONENTS or (include_companions and root != "hyprland"):
            raise PipelineError("Unsupported primary component selection")
        selected = {root: self.sources.acquire(root, candidate)}
        primaries = [root]
        if include_companions:
            for name in COMPANIONS:
                candidates = self.sources.releases(name, self.max_candidates)
                if not candidates:
                    raise PipelineError(f"No supported stable release found for {name}")
                selected[name] = self.sources.acquire(name, candidates[0])
                primaries.append(name)
        force = set(primaries)
        seen = set()
        for _ in range(50):
            requirements = {
                name: [normalize_requirement(value) for value in discover(source)]
                for name, source in selected.items()
            }
            combined = defaultdict(list)
            for dependencies in requirements.values():
                for requirement in dependencies:
                    if requirement.managed:
                        combined[requirement.managed].append(requirement)
            changed = False
            blockers = []
            resolutions = []
            force.update(name for name in combined if hypr_project(name))
            for consumer, dependencies in requirements.items():
                for requirement in dependencies:
                    providers = self.providers.find(requirement)
                    managed = requirement.managed
                    decision = {
                        **requirement.to_dict(),
                        "consumer": consumer,
                        "providers": providers,
                    }
                    if managed in selected or managed in force or (managed and not providers):
                        force.add(managed)
                        if managed not in selected or not all(
                            item.accepts(selected[managed]["version"])
                            for item in combined[managed]
                        ):
                            if managed == "hyprland-protocols":
                                candidates = [
                                    self.sources.protocols(source)
                                    for source in selected.values()
                                    if source.get("directory") and (
                                        Path(source["directory"]) / "subprojects/hyprland-protocols"
                                    ).is_dir()
                                ]
                                if not candidates:
                                    candidates = self.sources.releases(managed, self.max_candidates)
                            else:
                                candidates = self.sources.releases(managed, self.max_candidates)
                            acceptable = [
                                value for value in candidates
                                if all(
                                    item.accepts(value["version"])
                                    for item in combined[managed]
                                )
                            ]
                            if managed == "hyprland-protocols" and not acceptable:
                                acceptable = [
                                    value
                                    for value in self.sources.releases(managed, self.max_candidates)
                                    if all(
                                        item.accepts(value["version"])
                                        for item in combined[managed]
                                    )
                                ]
                            if not acceptable:
                                blockers.append({
                                    **decision,
                                    "reason": "No compatible supported stable source version",
                                })
                                continue
                            source = acceptable[0]
                            selected[managed] = (
                                source if managed == "hyprland-protocols" and "archive" in source
                                else self.sources.acquire(managed, source)
                            )
                            changed = True
                        decision["provider"] = "source"
                    elif providers:
                        decision["provider"] = "fedora"
                    else:
                        blockers.append({
                            **decision,
                            "available": self.providers.available(requirement.capability),
                            "reason": "Missing or incompatible unmanaged Fedora dependency",
                        })
                    resolutions.append(decision)
            signature = tuple(sorted((name, value["version"]) for name, value in selected.items()))
            if changed:
                if signature in seen:
                    raise PipelineError("Dependency selection oscillates; pin compatible releases")
                seen.add(signature)
                continue
            graph = {
                name: {
                    dependency.managed
                    for dependency in dependencies
                    if dependency.managed in selected
                }
                for name, dependencies in requirements.items()
            }
            return {
                "schema_version": 1,
                "source_policy": SOURCE_POLICY,
                "status": "blocked" if blockers else "resolved",
                "target": "fedora-44-x86_64",
                "root": root,
                "primaries": primaries,
                "sources": selected,
                "requirements": {
                    name: [value.to_dict() for value in dependencies]
                    for name, dependencies in requirements.items()
                },
                "resolutions": resolutions,
                "blockers": blockers,
                "order": topological_order(graph),
            }
        raise PipelineError("Dependency resolution exceeded its bounded iteration count")
