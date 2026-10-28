"""builder.derive.layers, moved from the hub: base layers from the registry manifest, lines from created_by."""

import json
from pathlib import Path

from builder.derive.layers import annotate, attribute, created_by_parts, parse_dockerfile

FIXTURES = Path(__file__).parent / "fixtures"

BUILD = json.loads((FIXTURES / "release" / "build.json").read_text())
LAYERS = json.loads((FIXTURES / "release" / "layers.amd64.json").read_text())
DOCKERFILE = BUILD["dockerfile"]["amd64"]
BASE = {"platforms": {"amd64": {"digest": "sha256:x", "layers": [layer["digest"] for layer in LAYERS[:7]]}}}


def test_parse_dockerfile_follows_continuations_and_skips_comments() -> None:
    instructions = parse_dockerfile(DOCKERFILE)
    assert [i.keyword for i in instructions] == [
        "FROM",
        "ENV",
        "ARG",
        "ARG",
        "ARG",
        "LABEL",
        "LABEL",
        "ENV",
        "RUN",
        "COPY",
        "EXPOSE",
        "VOLUME",
    ]
    run = next(i for i in instructions if i.keyword == "RUN")
    assert (run.start, run.end) == (21, 45) and run.text.startswith('RUN echo "**** install packages ****" && apk add')
    env = [i for i in instructions if i.keyword == "ENV"][1]
    assert (env.start, env.end) == (15, 19)


def test_created_by_parts_strips_buildkit_decorations() -> None:
    assert created_by_parts("COPY root/ / # buildkit") == ("COPY", "root/ /")
    assert created_by_parts("RUN |2 A=1 B= /bin/sh -c echo 'hi'  there") == ("RUN", "echo hi there")
    assert created_by_parts("/bin/sh -c #(nop)  ENV X=1") is None
    assert created_by_parts("#(nop) ENV X=1") == ("ENV", "X=1")


def test_real_release_with_the_base_from_the_registry() -> None:
    layers, meta = annotate(LAYERS, DOCKERFILE, BASE, "amd64")
    assert meta == {"base_layers": 7, "from_base_source": "registry"}
    assert [layer["from_base"] for layer in layers] == [True] * 7 + [False, False]
    assert [layer.get("dockerfile_lines") for layer in layers] == [None] * 7 + [[21, 45], [48, 48]]
    assert "from_base" not in LAYERS[0]  # the input is not modified


def test_without_the_base_the_last_layers_still_match_backwards() -> None:
    layers, meta = annotate(LAYERS, DOCKERFILE, None, "amd64")
    assert meta == {"base_layers": None, "from_base_source": None}
    assert all("from_base" not in layer for layer in layers)
    # The base's own `COPY root/ /` (layer 6) is not attributed to the image's COPY: that went to layer 8.
    assert [layer.get("dockerfile_lines") for layer in layers] == [None] * 7 + [[21, 45], [48, 48]]


def test_layer_count_fallback_when_only_another_arch_is_known() -> None:
    other = {"platforms": {"arm64": {"digest": "sha256:y", "layers": [f"sha256:{n:064d}" for n in range(7)]}}}
    layers, meta = annotate(LAYERS, DOCKERFILE, other, "amd64")
    assert meta == {"base_layers": 7, "from_base_source": "count"}
    assert [layer["from_base"] for layer in layers] == [True] * 7 + [False, False]


def test_harness_flags_stand_when_nothing_better_is_known() -> None:
    flagged = [{**layer, "from_base": n < 7} for n, layer in enumerate(LAYERS)]
    layers, meta = annotate(flagged, DOCKERFILE, None, "amd64")
    assert meta == {"base_layers": 7, "from_base_source": "harness"}
    assert layers[7]["dockerfile_lines"] == [21, 45]


def test_unconfident_matches_are_left_unset() -> None:
    dockerfile = "FROM x\nRUN apk add curl jq\nCOPY root/ /\n"
    layers = [
        {"index": 0, "digest": "a", "size": 1, "created_by": "RUN /bin/sh -c apk add curl"},
        {"index": 1, "digest": "b", "size": 1, "created_by": "COPY root/ / # buildkit"},
        {"index": 2, "digest": "c", "size": 1, "created_by": "COPY other/ /"},
    ]
    assert attribute(layers, dockerfile) == [None, [3, 3], None]
    assert attribute(layers, None) == [None, None, None]


def test_multi_stage_uses_the_final_stage_only() -> None:
    dockerfile = "FROM a AS build\nRUN make\nFROM b\nCOPY --from=build /out /\nRUN make\n"
    layers = [
        {"index": 0, "digest": "a", "size": 1, "created_by": "COPY --from=build /out / # buildkit"},
        {"index": 1, "digest": "b", "size": 1, "created_by": "RUN /bin/sh -c make"},
    ]
    assert attribute(layers, dockerfile) == [[4, 4], [5, 5]]
