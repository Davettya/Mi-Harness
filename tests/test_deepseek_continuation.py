from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.messages import messages_from_dict, messages_to_dict
from harness.model_gateway.deepseek import DeepSeekChatModel


def test_deepseek_output_limit_uses_documented_wire_name():
    model = DeepSeekChatModel(api_key="fixture", model="deepseek-flash", max_tokens=16384, use_responses_api=False)
    payload = model._get_request_payload("hello")
    assert payload["max_tokens"] == 16384
    assert "max_completion_tokens" not in payload
    override = model._get_request_payload("hello", max_tokens=32)
    assert override["max_tokens"] == 32
    assert "max_completion_tokens" not in override


def test_thinking_tool_and_final_turn_survive_archive_and_request():
    model = DeepSeekChatModel(api_key="fixture", model="deepseek-flash", use_responses_api=False)
    response = {"choices":[{"message":{"role":"assistant","content":"","reasoning_content":"private continuation","tool_calls":[{"id":"call_a","type":"function","function":{"name":"terminal","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}
    answer = model._create_chat_result(response).generations[0].message
    history = [HumanMessage(content="task"),answer,ToolMessage(content="ok",tool_call_id="call_a"),AIMessage(content="done",additional_kwargs={"reasoning_content":"final reasoning"})]
    restored = messages_from_dict(messages_to_dict(history))
    payload = model._get_request_payload(restored)
    assert payload["messages"][1]["reasoning_content"]=="private continuation"
    assert payload["messages"][3]["reasoning_content"]=="final reasoning"
    assert "reasoning_content" not in model._get_request_payload([AIMessage(content="legacy")])["messages"][0]


def test_streamed_reasoning_preserves_all_fragments():
    model = DeepSeekChatModel(api_key="fixture", model="deepseek-flash", use_responses_api=False)
    chunks = [model._convert_chunk_to_generation_chunk({"choices":[{"delta":{"role":"assistant","reasoning_content":text},"finish_reason":None}]},AIMessageChunk,None) for text in ["first ","second"]]
    combined = chunks[0]+chunks[1]
    assert combined.message.additional_kwargs["reasoning_content"]=="first second"


def test_smoke_budget_unknown_and_retries_share_cap(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("smoke",Path(__file__).parents[1]/"evals/terminal_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    ledger = smoke.Ledger(tmp_path/"budget.db")
    body = {"model":"deepseek-flash","reasoning_effort":"high","thinking":{"type":"enabled"},"max_tokens":16384}
    key = ledger.reserve("a",body)
    reserved = ledger.spent()
    ledger.settle(key,None)
    assert ledger.spent()==reserved
    ledger.settle(key,{"prompt_tokens":1000,"prompt_cache_hit_tokens":900,"completion_tokens":100})
    assert ledger.spent()==1036
    import pytest
    with pytest.raises(RuntimeError,match="BUDGET_EXHAUSTED"):
        ledger.reserve("b",{**body,"max_tokens":2_000_000})
    assert ledger.spent()==1036


async def test_smoke_large_output_uses_real_artifacts_and_scoped_readback(tmp_path):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    import pytest
    from harness.artifacts import ArtifactStore
    from harness.context import ContextService
    from harness.core import ExecutionContext, HarnessError
    from harness.model_gateway import demo_profile
    from harness.storage import Store
    spec = importlib.util.spec_from_file_location("smoke",Path(__file__).parents[1]/"evals/terminal_smoke.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    store = Store(tmp_path/"runtime.db")
    workspace = store.create_workspace("smoke","test",str(tmp_path),{})
    artifacts = ArtifactStore(store,tmp_path/"artifacts")
    ctx = ExecutionContext(owner_id="smoke",workspace_id=workspace["id"],session_id="s",branch_id="b",run_id="r",root_run_id="r",worker_attempt_id="a",fencing_token=1,graph_thread_key="t",input_revision=1,config_snapshot_id="c",trace_id="t")
    composer = ContextService(store,artifact_writer=artifacts.writer)
    content = "large tool output\n"*1500
    messages = [HumanMessage(id="u",content="task"),AIMessage(id="a",content="",tool_calls=[{"id":"call","name":"terminal","args":{}}]),ToolMessage(id="o",tool_call_id="call",content=content)]
    await composer.compose(ctx,messages,demo_profile(),[])
    offload = store.get("context_offloads","t:o")
    identity = offload["artifact_ref"]["artifact_id"]
    assert artifacts.read_range(identity,"smoke",0,len(content))==content.encode()
    tools = smoke.ContainerTools("unused",store,tmp_path,artifacts)
    result = await tools.execute(ctx,SimpleNamespace(tool_call_id="read"),"artifact_read",{"artifact_id":identity,"offset":10,"length":20})
    assert result.summary==content[10:30] and result.truncated
    with pytest.raises(HarnessError):
        await tools.execute(ctx.model_copy(update={"workspace_id":"other"}),SimpleNamespace(tool_call_id="denied"),"artifact_read",{"artifact_id":identity})
