# Runtime / Model / Context compatibility evidence

Checked on 2026-09-17 using Python 3.11.9 on Windows 10.0.26200.
Initial PoC dependency lock SHA256: `a9d71a3f429254e32aa4bd00353742868af54a5c91d06489fa68cd5a2dd9c732`.

| Package | Locked version |
| --- | --- |
| langchain | 1.4.1 |
| langgraph | 1.2.11 |
| langgraph-checkpoint-sqlite | 3.1.1 |
| langchain-openai | 1.6.2 |
| langchain-anthropic | 1.7.2 |
| langchain-ollama | 1.1.0 |

The code uses the inspected installed `create_agent`, `AgentMiddleware.awrap_model_call`,
`AgentMiddleware.awrap_tool_call`, `ModelRequest.override`, `AsyncSqliteSaver`,
`CompiledStateGraph.ainvoke(..., durability="sync")`, `interrupt`, and
`Command(resume={interrupt_id: resolution})` APIs. There is no second model/tool loop.

Primary references consulted:

- [LangChain custom middleware and hook order](https://docs.langchain.com/oss/python/langchain/middleware/custom)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupt semantics](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangChain standard multimodal messages](https://docs.langchain.com/oss/python/langchain/messages)

## Executed evidence

`python -m pytest tests/test_model_providers.py tests/test_runtime_context_gateway.py -q`
initial integration run: **16 passed in 25.20 seconds**. Subsequent full-suite runs are
the current acceptance source; this is a recorded module milestone, not a release claim.

| Design check | Executed boundary and assertion |
| --- | --- |
| POC01 / R05 | Actual create_agent + SQLite saver. Hook before saving model response fails: zero tool executions and no complete assistant persisted. Hook after model checkpoint/before tool fails: recovery executes original persisted tool-call ID. |
| R03 | Tool ledger commits, graph result checkpoint hook fails. New Runtime reads same SQLite saver and ledger; total effect executions remain one. |
| R01 | Approval interrupt persists; saver and Runtime instances close/reopen. Explicit interrupt-ID resolution resumes same operation identity. |
| M01 protocol fixtures | Three actual provider SDK adapters talk to isolated loopback HTTP fixtures. OpenAI Chat Completions, Anthropic Messages, Ollama Chat each execute assistant tool request / injected tool result / final assistant protocol. This verifies adapters against fixtures, **not live vendors or a running Ollama model**. |
| M02 | Partial JSON chunks are strictly validated with json.loads before delivery; attempt IDs isolate buffers; absent stream completion marker is rejected; no partial chunks escape. |
| M03 | Retry requests have different attempt IDs; each settles separately; lost usage stays unknown and cost null; cancellation prevents another request. |
| C01 | Exact independent/joint-window arithmetic; request includes tools/system/profile/output limit; over-budget mandatory input stops with an explicit reason; immutable source messages remain archived. |
| C03 | New input revision during summarization causes failed CAS; inactive summary may remain but the active pointer never overwrites the newer input. |
| CX04 | Unreviewed proposal never enters retrieval, owner/workspace filtering precedes ranking, stale update fails, owner change denied, deletion immediately removes retrieval visibility. |

## Explicit acceptance limits

- Live provider models, credentials, vendor limits/prices, reasoning continuation formats and
  real image/structured-output capabilities require separately recorded profile-specific tests.
  No external profile is automatically declared verified. The deterministic demo is labelled in
  every response and has fixture-only evidence.
- The model stream buffers tool arguments through complete response validation. Text-only deltas
  are published on the separate ephemeral channel with stable message/attempt/chunk identities;
  durable message commits replace drafts and do not share the temporary stream cursor.
- Recovery fault hooks exercise real graph and saver boundaries within a controlled process.
  These tests are not a substitute for the release suite's OS-level process kill/backup tests.
- Summary semantic quality requires the configured summary model and the fixed task corpus.
  Module tests verify schema, exact protected constraints, provenance and revision consistency.
  A model-free demo does not claim general-purpose semantic summarization.
- Keyword memory retrieval is implemented; no vector index or embedding service is required.
  The current source-of-truth read avoids a stale secondary cache.

## Host wiring

`tools_factory(ctx, AgentSpec)` returns schema-only `BaseTool` wrappers. Every execution is
intercepted and delegated to `ToolGateway.execute(ctx, OperationKey, name, args)`.
`artifact_writer(ctx, bytes, mime_type, provenance_ref)` returns a complete `ArtifactRef`.
`message_commit(ctx, message_id, role, public_projection)` receives only persisted messages;
provider continuation metadata remains in the protected context archive/checkpoint.
`ModelGateway.aclose()` releases provider connections during Host shutdown.

Later integration added the production Worker, transactional model permits and unknown-usage
retention, steering checkpoint-before-ack, real graph-history forks, pending approval/input and
single-slot child wakeups. `tests/test_integration.py` passed all three vertical scenarios before
the final full regression. Actual MCP MRTR continuation tests additionally confirmed that
LangGraph can reuse both framework interrupt ID and checkpoint for successive waits in one task.
The durable Host mapping therefore also contains the logical interaction/wait/operation reference;
the owner contract is recorded in `docs/implementation/05-runtime.md`.

Provider transports disable ambient proxy variables; LangChain/LangGraph and model calls disable
ambient LangSmith tracing. The locked Anthropic SDK uses `httpx2`, while OpenAI uses `httpx`; both
explicit clients are covered by the loopback protocol fixture suite. Production summaries select
the fixed snapshot's summary profile (or its main profile), and pinned constraints participate in
composition, source revisions and compaction CAS. The demo summary remains a deterministic fixture.

## Combined regression on 2026-09-17

Command:

```text
.venv/Scripts/python.exe -m pytest tests/test_runtime_context_gateway.py tests/test_model_providers.py tests/test_scheduler.py tests/test_integration.py tests/test_mcp_continuation.py -q
```

Result: **28 passed in 120.64 seconds**. Dependency lock SHA256 for this regression:
`55e6d7457165a00d795664a58bf3ec2ec70d82dcc66a06a23b310d82416c9829`.
The suite includes actual installed LangChain/LangGraph and provider SDKs with deterministic
fixtures; MCP continuation uses the real MCP SDK over the two tested HTTP response modes.

- R01 includes sequential approval then MRTR elicitation in the same framework task, durable
  Host ID mapping, and close/reopen recovery. Scheduler tests cover partial multi-interaction
  answers, expiry without approval, stale fencing and single wakeup.
- A01 and UI02 include the production composition/Worker path with one worker slot, a child
  result, a safe steering boundary, and a fork containing the source graph's working history.
- A02 includes shared model permits and unknown consumption remaining reserved. Concurrent
  child-creation cancellation races require the scheduler's separate acceptance evidence.
- C02/C03 include configured production summary profile selection, retained exact constraints,
  immutable pin references and failure of summary activation when the pin revision changes.
- SEC02 includes a remote-tracer canary around the outermost model invocation; ambient tracing
  does not instantiate an export handler for direct/diagnostic calls or streams.

This evidence does not measure real-model task effectiveness, multimodal provider compatibility,
the 30–50 task corpus, 8-hour stability or the performance percentiles in 13. These remain separate
release gates. Per-lease Runtime caches are now released after settlement; saver and archive data
remain available for later inspection and recovery.

The final attachment increment was separately checked with
`tests/test_runtime_context_gateway.py tests/test_model_providers.py`: **20 passed in 9.26 seconds**.
The three installed provider SDK fixture cases now also assert actual wire image fields for the
same standard base64 content block. This verifies protocol mapping, not image understanding.
Context tests verify bounded UTF-8 attachment reads, immutable raw references, material provenance,
rejection before reading unverified images and explicit rejection of binary extraction.
PDF/DOCX extraction is unsupported. PNG/JPEG/WebP/GIF require a verified vision profile and are
bounded to 10 MiB before the request's conservative token limit. Images remain active when textual
history is compacted; the summary explicitly does not claim to have interpreted their pixels.

The remaining final vertical regression,
`tests/test_integration.py tests/test_mcp_continuation.py tests/test_scheduler.py`, completed with
**9 passed in 110.11 seconds**. This includes real UTF-8 upload-to-run content, copied branch pins,
the updated publish-after-fork ordering, cache release after settlement, and MCP continuation after
releasing the previous lease's Runtime graph. Combined with the attachment increment above, all
**29 current cases** passed. Scoped Ruff checks also passed. The root release report owns the
full-repository smoke run and later lock/code revisions.
