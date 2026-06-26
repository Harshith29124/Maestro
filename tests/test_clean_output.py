from maestro.providers.base import clean_output


def test_strips_think_block():
    out = clean_output("<think>let me count s-t-r-a-w...</think>The answer is 3.")
    assert "think" not in out.lower()
    assert out == "The answer is 3."


def test_strips_reasoning_and_thought_variants():
    assert clean_output("<reasoning>x</reasoning>hi") == "hi"
    assert clean_output("<Thought>y</Thought>done") == "done"


def test_strips_unclosed_leading_think():
    # Truncated output: model ran out of tokens mid-thought.
    out = clean_output("<think>counting the letters one by one and tallying each")
    assert out == ""


def test_strips_scaffolding_labels():
    out = clean_output("STRATEGY USED: break it down\nThe final answer.")
    assert "STRATEGY" not in out
    assert "The final answer." in out


def test_plain_text_untouched():
    assert clean_output("Just a normal answer.") == "Just a normal answer."
