"""The registry's graph builder must stay byte-identical to the backend's build_workflow()."""

import pytest

from atelier.workflows import GenParams, qwen_image_21


@pytest.mark.parametrize(
    "p",
    [
        GenParams(prompt="a red fox in snow", negative="", width=1088, height=1920, steps=25, cfg=1.0, seed=1),
        GenParams(
            prompt="phố cổ Hội An về đêm",
            negative="blurry, text",
            width=1328,
            height=1328,
            steps=8,
            cfg=2.5,
            seed=2**31 - 1,
        ),
    ],
)
def test_graph_matches_backend_build_workflow(backend_script, p):
    expected = backend_script.build_workflow(p.prompt, p.width, p.height, p.steps, p.seed, p.cfg, p.negative)
    assert qwen_image_21.build_graph(p) == expected
