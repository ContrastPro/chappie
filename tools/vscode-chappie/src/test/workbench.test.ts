import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { ControlPlaneModelV1, DevelopmentAnalysisV1, ProjectQAResponseV1 } from "../api/contracts";
import { workbenchMarkdownRendererScript } from "../ui/markdown";
import { WORKBENCH_COMPOSER_MAX_HEIGHT_PX, WORKBENCH_COMPOSER_MIN_HEIGHT_PX, workbenchComposerSize } from "../ui/composer";
import { parseWebviewMessage, WORKBENCH_PROTOCOL_VERSION } from "../ui/workbench_protocol";
import { workbenchRendererScript } from "../ui/workbench_render";
import { MAX_WORKBENCH_TURNS, WorkbenchSessionV1 } from "../ui/workbench_state";
import { roleLabel, roleUnavailableMessage, selectRole } from "../ui/model_roles";

const noContext = { includeFile: false, includeSelection: false, includeGit: false };
const roles: ControlPlaneModelV1[] = [
  { model_id: "QWEN3_4B", concrete_model: "qwen3:4b", installed: true, ready: true, default: true, role: "FAST", profile_identity: "FAST", capability_identity: "development_model_role/FAST", availability: "AVAILABLE" },
  { model_id: "QWEN3_CODER_30B", concrete_model: "qwen3-coder:30b", installed: true, ready: true, default: false, role: "CODE", profile_identity: "CODE_ADVISORY", capability_identity: "development_model_role/CODE_ADVISORY", availability: "AVAILABLE" },
  { model_id: "QWEN3_14B", concrete_model: "qwen3:14b", installed: true, ready: true, default: false, role: "DEEP", profile_identity: "DEEP", capability_identity: "development_model_role/DEEP", availability: "AVAILABLE" },
];
const fileAttachment = { id: "context_1", kind: "includeFile" as const, label: "File · app/runtime/application.py", detail: "app/runtime/application.py · Lines 1–4", preview: "app/runtime/application.py · Lines 1–4\n\nclass Application:", characterCount: 19, truncated: false };

const response = (answer: string): ProjectQAResponseV1 => ({
  identity: "chappie_control_plane/1.2.0",
  contract_digest: "a".repeat(64),
  state: "OK",
  question: "question",
  answer,
  supporting_knowledge_fact_ids: [],
  supporting_records: [],
  unknowns: [],
  context_digest: "b".repeat(64),
  provider: "mock",
  model: "mock",
  authority: "AI_WITH_KNOWLEDGE_READ_ONLY",
  validation_status: "VALID",
  error: null,
});

test("Workbench protocol accepts only bounded typed webview messages", () => {
  assert.deepEqual(parseWebviewMessage({ type: "ready", protocol: WORKBENCH_PROTOCOL_VERSION }), { type: "ready", protocol: WORKBENCH_PROTOCOL_VERSION });
  assert.deepEqual(parseWebviewMessage({ type: "ask", requestId: "request_1", question: "Explain architecture", context: { includeFile: true, includeSelection: false, includeGit: true } }), { type: "ask", requestId: "request_1", question: "Explain architecture", context: { includeFile: true, includeSelection: false, includeGit: true } });
  assert.equal(parseWebviewMessage({ type: "ask", requestId: "request_1", question: " ", context: {} }), undefined);
  assert.equal(parseWebviewMessage({ type: "ask", requestId: "request_1", question: "x".repeat(1001), context: {} }), undefined);
  assert.equal(parseWebviewMessage({ type: "ask", requestId: "request_1", question: "x".repeat(12_001), context: noContext }), undefined);
  assert.equal(parseWebviewMessage({ type: "runAction", action: "unsafe" }), undefined);
  assert.deepEqual(parseWebviewMessage({ type: "retry", turnId: "turn_1" }), { type: "retry", turnId: "turn_1" });
  assert.deepEqual(parseWebviewMessage({ type: "feedback", turnId: "turn_1", value: "good" }), { type: "feedback", turnId: "turn_1", value: "good" });
  assert.equal(parseWebviewMessage({ type: "feedback", turnId: "turn_1", value: "maybe" }), undefined);
  assert.equal(parseWebviewMessage({ type: "ready", protocol: "wrong" }), undefined);
  assert.deepEqual(parseWebviewMessage({ type: "selectModelRole" }), { type: "selectModelRole" });
});

test("Workbench displays backend-projected stable roles without owning model policy", () => {
  assert.equal(roleLabel(selectRole(roles, "FAST")), "FAST — qwen3:4b");
  assert.equal(roleLabel(selectRole(roles, "CODE")), "CODE — qwen3-coder:30b");
  assert.equal(roleLabel(selectRole(roles, "DEEP")), "DEEP — qwen3:14b");
  const remapped = [{ ...roles[1], concrete_model: "backend-remapped-coder" }];
  assert.equal(roleLabel(selectRole(remapped, "CODE")), "CODE — backend-remapped-coder");
  assert.match(roleUnavailableMessage(selectRole([{ ...roles[1], ready: false, availability: "UNAVAILABLE" }], "CODE")) ?? "", /will not fall back automatically/);
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as { contributes: { configuration: { properties: Record<string, { scope?: string }> } } };
  assert.equal(manifest.contributes.configuration.properties["chappie.selectedModelRole"].scope, "resource");
  const host = readFileSync("src/extension.ts", "utf8");
  assert.match(host, /ConfigurationTarget\.Workspace/);
  assert.match(host, /askProject\(question, collectAttachedWorkbenchContext\(options, attachments, workbenchSnapshots\), role\)/);
  assert.doesNotMatch(host, /ollama pull|selectedModelRole.*\.env/);
});

test("Workbench session permits one active request and preserves it when trimming", () => {
  const session = new WorkbenchSessionV1();
  const first = session.begin("turn_1", "request_1", "First", noContext);
  assert.ok(first);
  assert.equal(session.begin("turn_2", "request_2", "Second", noContext), undefined);
  session.fail("turn_1", "Unavailable");
  for (let index = 2; index <= MAX_WORKBENCH_TURNS; index += 1) {
    const turn = session.begin(`turn_${index}`, `request_${index}`, String(index), noContext);
    assert.ok(turn);
    session.complete(turn.turnId, response(String(index)));
  }
  const active = session.begin("turn_active", "request_active", "Active", noContext);
  assert.ok(active);
  const snapshot = session.snapshot();
  assert.ok(snapshot.length <= MAX_WORKBENCH_TURNS);
  assert.equal(snapshot.find((turn) => turn.status === "pending")?.turnId, "turn_active");
  assert.equal(snapshot.some((turn) => turn.turnId === "turn_1"), true);
});

test("Workbench clear keeps an in-flight request and returned snapshots are detached", () => {
  const session = new WorkbenchSessionV1();
  const pending = session.begin("turn_1", "request_1", "Question", noContext);
  assert.ok(pending);
  const snapshot = session.snapshot();
  snapshot[0].question = "Changed outside";
  assert.equal(session.snapshot()[0].question, "Question");
  session.clear();
  assert.equal(session.snapshot().length, 1);
  assert.equal(session.snapshot()[0].turnId, "turn_1");
});

test("Workbench turns freeze typed attachment snapshots independently from later composer state", () => {
  const session = new WorkbenchSessionV1();
  const turn = session.begin("turn_context", "request_context", "Question", { includeFile: true, includeSelection: false, includeGit: false }, 100, [fileAttachment]);
  assert.ok(turn);
  fileAttachment.label = "Changed outside";
  const snapshot = session.find(turn.turnId);
  assert.equal(snapshot?.attachments[0]?.label, "File · app/runtime/application.py");
  assert.equal(snapshot?.attachments[0]?.preview, "app/runtime/application.py · Lines 1–4\n\nclass Application:");
  assert.equal(snapshot?.attachments[0]?.id, "context_1");
});

test("Workbench terminal snapshots make completed and failed turns internally consistent", () => {
  const session = new WorkbenchSessionV1();
  const successful = session.begin("turn_success", "request_success", "Question", noContext, 100);
  assert.ok(successful);
  const realShape = response("A grounded final answer.");
  realShape.supporting_records = [{ record_id: "architecture", title: "Chappie Architecture", authority: "DOCUMENTATION", status: "ACTIVE" }];
  session.complete(successful.turnId, realShape, 200);
  const completed = session.find(successful.turnId);
  assert.equal(completed?.requestId, "request_success");
  assert.equal(completed?.status, "complete");
  assert.equal(completed?.response?.answer, "A grounded final answer.");
  assert.equal(completed?.response?.supporting_records[0]?.record_id, "architecture");
  assert.equal(completed?.completedAtMs, 200);

  const rejected = session.begin("turn_failure", "request_failure", "Question", noContext, 300);
  assert.ok(rejected);
  session.fail(rejected.turnId, "Knowledge is stale. Repair Chappie to use Project QA.", 400);
  const failed = session.find(rejected.turnId);
  assert.equal(failed?.requestId, "request_failure");
  assert.equal(failed?.status, "failed");
  assert.equal(failed?.response, undefined);
  assert.match(failed?.error ?? "", /Knowledge is stale/);
});

test("Development failure diagnostics remain attached to their own Workbench turns", () => {
  const session = new WorkbenchSessionV1();
  const first = session.begin("turn_a", "request_a", "Sensitive request A", noContext, 100, [], "DEVELOPMENT");
  assert.ok(first);
  const failureA = { requestId: "a".repeat(32), reasonCode: "PROPOSAL_SCHEMA_INVALID", failedStage: "PROPOSAL_PARSE", category: "PROPOSAL" as const, completedStages: ["REQUEST_ACCEPTED", "GROUNDING_PARSE"], lifecycle: [{ stage: "PROPOSAL_PARSE", status: "FAILED", elapsed_ms: 3, reason_code: "PROPOSAL_SCHEMA_INVALID" }], requestedRole: "CODE", effectiveModel: "qwen3-coder:30b", readiness: null, handoffStatus: "NOT_REACHED" as const };
  session.fail(first.turnId, "Development proposal could not be validated.", 110, failureA);
  const second = session.begin("turn_b", "request_b", "Sensitive request B", noContext, 120, [], "DEVELOPMENT");
  assert.ok(second);
  const failureB = { ...failureA, requestId: "b".repeat(32), reasonCode: "AI_UNAVAILABLE", failedStage: "ROLE_RESOLUTION", category: "DEPENDENCY" as const, lifecycle: [{ stage: "ROLE_RESOLUTION", status: "FAILED", elapsed_ms: 1, reason_code: "AI_UNAVAILABLE" }] };
  session.fail(second.turnId, "Chappie AI model is currently unavailable for Development Analysis.", 130, failureB);

  const retainedA = session.find(first.turnId)?.developmentFailure;
  const retainedB = session.find(second.turnId)?.developmentFailure;
  assert.equal(retainedA?.requestId, "a".repeat(32));
  assert.equal(retainedA?.reasonCode, "PROPOSAL_SCHEMA_INVALID");
  assert.equal(retainedA?.failedStage, "PROPOSAL_PARSE");
  assert.equal(retainedB?.requestId, "b".repeat(32));
  assert.equal(retainedB?.reasonCode, "AI_UNAVAILABLE");
  assert.equal(retainedB?.failedStage, "ROLE_RESOLUTION");
  const diagnostics = JSON.stringify([retainedA, retainedB]);
  assert.doesNotMatch(diagnostics, /Sensitive request [AB]|prompt|provider payload|raw knowledge/i);
});

test("Workbench rejects a completed lifecycle snapshot without a final answer", () => {
  const session = new WorkbenchSessionV1();
  const pending = session.begin("turn_1", "request_1", "Question", noContext, 100);
  assert.ok(pending);
  assert.throws(() => session.complete(pending.turnId, response("   "), 200), /non-empty Project QA answer/);
  assert.equal(session.find(pending.turnId)?.status, "pending");
});

test("production-shaped replaceSession projects a terminal turn over the same pending turnId", () => {
  const session = new WorkbenchSessionV1();
  const pending = session.begin("turn_real", "request_real", "Understand architecture", noContext, 100);
  assert.ok(pending);
  const pendingSnapshot = session.snapshot();
  const pendingRevision = session.currentRevision;
  const terminalResponse = response("Chappie Architecture defines the system boundaries.");
  terminalResponse.supporting_records = [{ record_id: "architecture", title: "Chappie Architecture", authority: "DOCUMENTATION", status: "ACTIVE" }];
  session.complete(pending.turnId, terminalResponse, 200);
  const terminalSnapshot = session.snapshot();
  const terminalRevision = session.currentRevision;

  assert.equal(pendingSnapshot[0].turnId, terminalSnapshot[0].turnId);
  assert.equal(pendingSnapshot[0].requestId, terminalSnapshot[0].requestId);
  assert.equal(pendingSnapshot[0].status, "pending");
  assert.equal(terminalSnapshot[0].status, "complete");
  assert.match(terminalSnapshot[0].response?.answer ?? "", /system boundaries/);
  assert.equal(terminalSnapshot[0].response?.supporting_records[0]?.title, "Chappie Architecture");
  assert.equal(terminalSnapshot[0].response?.authority, "AI_WITH_KNOWLEDGE_READ_ONLY");
  assert.equal(terminalSnapshot[0].response?.validation_status, "VALID");
  assert.ok(terminalRevision > pendingRevision);
});

test("inline composer sizing is compact, grows, caps, and shrinks deterministically", () => {
  assert.deepEqual(workbenchComposerSize(0), { heightPx: WORKBENCH_COMPOSER_MIN_HEIGHT_PX, overflowY: "hidden" });
  assert.deepEqual(workbenchComposerSize(84), { heightPx: 84, overflowY: "hidden" });
  assert.deepEqual(workbenchComposerSize(WORKBENCH_COMPOSER_MAX_HEIGHT_PX), { heightPx: WORKBENCH_COMPOSER_MAX_HEIGHT_PX, overflowY: "hidden" });
  assert.deepEqual(workbenchComposerSize(WORKBENCH_COMPOSER_MAX_HEIGHT_PX + 1), { heightPx: WORKBENCH_COMPOSER_MAX_HEIGHT_PX, overflowY: "auto" });
  assert.deepEqual(workbenchComposerSize(60), { heightPx: 60, overflowY: "hidden" });
});

test("Workbench eviction prefers completed turns over failed turns", () => {
  const session = new WorkbenchSessionV1();
  const failed = session.begin("failed", "request_failed", "Failed", noContext);
  assert.ok(failed);
  session.fail(failed.turnId, "Unavailable");
  for (let index = 1; index <= MAX_WORKBENCH_TURNS; index += 1) {
    const turn = session.begin(`complete_${index}`, `request_complete_${index}`, String(index), noContext);
    assert.ok(turn);
    session.complete(turn.turnId, response(String(index)));
  }
  const snapshot = session.snapshot();
  assert.equal(snapshot.some((turn) => turn.turnId === "failed"), true);
  assert.equal(snapshot.some((turn) => turn.turnId === "complete_1"), false);
});

test("Workbench renderer uses incremental DOM-safe turn rendering", () => {
  const script = workbenchRendererScript();
  assert.match(script, /textContent/);
  assert.match(script, /\.append\(/);
  assert.match(script, /replaceChildren/);
  assert.doesNotMatch(script, /innerHTML/);
  assert.match(script, /Preview '\+item\.label\+' context/);
  assert.match(script, /Remove '\+item\.label\+' context/);
  assert.match(script, /turnContextSummary/);
  assert.match(script, /preview\.remove\(\)/);
  assert.match(script, /clearConversation/);
  assert.match(script, /aria-expanded/);
  assert.match(script, /source-list/);
  assert.match(script, /navigator\.clipboard/);
  assert.match(script, /scrollHeight-canvas\.scrollTop/);
  assert.match(script, /attachments\.some/);
  assert.match(script, /expandedQuestions/);
  assert.match(script, /Show less/);
  assert.match(script, /aria-expanded/);
  assert.match(script, /\.turn\+\.turn/);
  assert.match(script, /composer-region\{border-top:0/);
});

test("Workbench context UI makes captured attachments explicit without webview filesystem authority", () => {
  const script = workbenchRendererScript();
  const html = readFileSync("src/ui/workbench_html.ts", "utf8");
  const host = readFileSync("src/extension.ts", "utf8");
  assert.match(html, /Add context/);
  assert.match(html, /Attached/);
  assert.match(html, /id="preview"/);
  assert.match(script, /item\.characterCount/);
  assert.match(script, /type:'previewContext',kind:item\.kind/);
  assert.match(script, /attachmentsSection\.hidden=!attachments\.length/);
  assert.match(script, /turn\.attachments/);
  assert.match(host, /workbenchSnapshots/);
  assert.match(host, /capturedWorkbenchAttachment/);
  assert.match(host, /collectAttachedWorkbenchContext/);
  assert.doesNotMatch(script, /node:fs|node:child_process|execFileSync/);
});

test("Workbench uses a compact composer and a dedicated read-only context preview panel", () => {
  const renderer = workbenchRendererScript();
  const host = readFileSync("src/ui/workbench.ts", "utf8");
  assert.match(renderer, /preview\.remove\(\)/);
  assert.match(renderer, /attachmentsSection\.hidden=!attachments\.length/);
  assert.match(renderer, /footer\.replaceChildren\(contextActions,footerSpacer,footerActions\)/);
  assert.match(renderer, /open\.onclick=\(\)=>post\(\{type:'previewContext',kind:item\.kind\}\)/);
  assert.match(renderer, /remove\.onclick=event=>\{event\.stopPropagation\(\);post\(\{type:'removeContext'/);
  assert.match(renderer, /if\(data\.message\).*else clearNotice\(\)/);
  assert.match(host, /createWebviewPanel\("chappie\.contextPreview"/);
  assert.match(host, /this\.contextPreviewPanel\.webview\.html = contextPreviewHtml/);
  assert.match(host, /enableScripts: false, localResourceRoots: \[\]/);
  assert.match(host, /const escape = \(value: string\): string =>/);
  assert.match(host, /<pre>\$\{escape\(attachment\.preview\)\}<\/pre>/);
  assert.match(host, /CONTEXT_PICKER_CANCELLED/);
  assert.match(host, /if \(error instanceof Error && error\.message === "CONTEXT_PICKER_CANCELLED"\) return/);
  assert.match(host, /this\.post\(\{ type: "notice", message: "" \}\)/);
});

test("Workbench keeps all composer actions in one bottom bar while attachments stay above it", () => {
  const renderer = workbenchRendererScript();
  assert.match(renderer, /contextLabel\.remove\(\)/);
  assert.match(renderer, /attachmentsLabel\.remove\(\)/);
  assert.match(renderer, /footer\.replaceChildren\(contextActions,footerSpacer,footerActions\)/);
  assert.match(renderer, /contextActions\.setAttribute\('aria-label','Add workspace context'\)/);
  assert.match(renderer, /button\.setAttribute\('aria-label','Attach '\+name\+' context'\)/);
  assert.match(renderer, /attachmentsSection\.hidden=!attachments\.length/);
  assert.match(renderer, /attachmentsSection\.append\(attachmentsNode\)/);
  assert.match(renderer, /\.composer-footer\{display:flex;align-items:center;gap:8px;width:100%;min-width:0;margin-top:7px;flex-wrap:wrap\}/);
  assert.match(renderer, /\.footer-actions\{display:flex;align-items:center;gap:6px;margin-left:auto;flex:0 0 auto\}/);
});

test("Workbench context protocol version and host snapshot retry path are intentional", () => {
  const source = readFileSync("src/ui/workbench.ts", "utf8");
  assert.equal(WORKBENCH_PROTOCOL_VERSION, "chappie_workbench/1.4.0");
  assert.match(source, /const attachments = message\.type === "retry" \? original!\.attachments : this\.attachmentSnapshot\(\)/);
  assert.match(source, /this\.dependencies\.askProject\(turn\.question, turn\.context, turn\.attachments, this\.connection\.modelRole\)/);
  assert.match(source, /selectModelRole/);
  assert.match(source, /if \(message\.type === "ask"\) this\.clearAttachments\(\)/);
  assert.match(source, /this\.attachments\.delete\("includeSelection"\)/);
  assert.match(source, /this\.attachments\.delete\("includeFile"\)/);
});

test("Inspector is a floating responsive surface and close collapses its grid slot", () => {
  const source = readFileSync("src/ui/workbench_html.ts", "utf8");
  assert.match(source, /class=\"shell inspector-open\"/);
  assert.match(source, /--chappie-surface-radius:10px/);
  assert.match(source, /column-gap:var\(--chappie-workbench-gap\)/);
  assert.match(source, /padding-right:var\(--chappie-workbench-gap\)/);
  assert.match(source, /\.shell:has\(\.inspector\[hidden\]\)/);
  assert.match(source, /\.shell \.inspector\[hidden\]\{display:none\}/);
  assert.match(source, /border-radius:var\(--chappie-surface-radius\)/);
  assert.match(source, /\.inspector-body\{flex:1;min-height:0/);
  assert.match(source, /@media\(max-width:1040px\)/);
});

test("Workbench Markdown subset builds inert DOM nodes for untrusted text", () => {
  const script = workbenchMarkdownRendererScript();
  assert.match(script, /document\.createElement/);
  assert.match(script, /textContent/);
  assert.match(script, /code-block/);
  assert.match(script, /blockquote/);
  assert.match(script, /startsWith\(fence\)/);
  assert.match(script, /const renderMarkdown=\(target,value\)=>target\.replaceChildren\(\.\.\.markdownNodes\(value\)\)/);
  assert.doesNotMatch(script, /innerHTML|insertAdjacentHTML|eval\(/);
  assert.doesNotMatch(script, /https?:\/\//);
});

test("completed Workbench turns call the Markdown renderer defined by the embedded Webview script", () => {
  const renderer = workbenchRendererScript();
  assert.match(renderer, /renderMarkdown\(answer,turn\.response\?\.answer\|\|''\)/);
  assert.match(renderer, /const renderMarkdown=\(target,value\)=>target\.replaceChildren\(\.\.\.markdownNodes\(value\)\)/);
});

test("Source disclosures remain DOM-local while canonical session snapshots are rendered", () => {
  const script = workbenchRendererScript();
  const html = readFileSync("src/ui/workbench_html.ts", "utf8");
  assert.match(script, /expandedSources=new Set/);
  assert.match(script, /const sourceDisclosure=/);
  assert.match(script, /Sources · /);
  assert.match(script, /list\.hidden=!open/);
  assert.match(html, /\.source-list\[hidden\]\{display:none\}/);
  assert.match(html, /\.turn \.sources\{margin-top:16px\}/);
  assert.match(html, /\.turn \.source-list\{margin-top:9px;gap:8px/);
  assert.match(script, /turns\.replaceChildren\(\.\.\.session\.map\(turnNode\)\)/);
  assert.match(script, /const patchFeedback=turn/);
  assert.match(script, /const renderSession=\(next,revision\)/);
});

test("Workbench message actions remain local, accessible, and preserve canonical turn history", () => {
  const script = workbenchRendererScript();
  assert.match(script, /const copyAction=/);
  assert.match(script, /Copy message/);
  assert.match(script, /Copy answer/);
  assert.match(script, /Edit message/);
  assert.match(script, /question\.value=turn\.question/);
  assert.match(script, /navigator\.clipboard\.writeText/);
  assert.match(script, /type:'retry'/);
  assert.match(script, /Good answer/);
  assert.match(script, /Not good/);
  assert.match(script, /turn\.status==='failed'/);
  assert.match(script, /const jumpLatest=/);
  assert.match(script, /canvas\.addEventListener\('scroll',updateJumpLatest\)/);
});

test("Inspector is closed initially and its header toggle opens it without affecting model selection", () => {
  const script = workbenchRendererScript();
  const html = readFileSync("src/ui/workbench_html.ts", "utf8");
  assert.match(script, /let inspectorOpen=false/);
  assert.match(script, /const setInspectorOpen=open=>/);
  assert.match(script, /newChat\.after\(toggle\)/);
  assert.match(script, /toggle\.onclick=\(\)=>setInspectorOpen\(!inspectorOpen\)/);
  assert.match(script, /close\.onclick=\(\)=>setInspectorOpen\(false\)/);
  assert.match(script, /close\.onclick=\(\)=>setInspectorOpen\(false\);setInspectorOpen\(false\)/);
  assert.match(script, /type:'selectModelRole'/);
  assert.match(script, /aria-label','Toggle Inspector'/);
  assert.match(script, /title='Toggle Inspector'/);
  assert.match(html, /\.header \.open-inspector\{display:inline-flex/);
});

test("Workbench exposes real retry feedback and bounded Inspector metadata", () => {
  const script = workbenchRendererScript();
  const host = readFileSync("src/extension.ts", "utf8");
  assert.match(script, /type:'retry'/);
  assert.match(script, /type:'feedback'/);
  assert.match(script, /turn\.response\?\.authority/);
  assert.match(script, /turn\.response\?\.sources/);
  assert.match(script, /const renderInspectorFailureAware=/);
  assert.match(script, /FAILED/);
  assert.match(script, /renderInspectorFailureAware\(\)/);
  assert.match(script, /const developmentFailureLines=/);
  assert.match(script, /Status: FAILED/);
  assert.match(script, /Request ID: /);
  assert.match(script, /Failed stage: /);
  assert.match(script, /Completed stages: /);
  assert.match(script, /Handoff: /);
  assert.match(host, /Development failure request_id=\$\{error\.requestId/);
  assert.match(host, /reason_code=\$\{error\.reasonCode\}/);
  assert.match(host, /failed_stage=\$\{diagnostic\?\.failed_stage/);
  assert.match(script, /No supported Chappie–Codex runtime integration is configured/);
  assert.doesNotMatch(script, /chain.of.thought|fake agent|fake market/i);
});

test("Workbench feedback is local typed state and retry source remains immutable", () => {
  const session = new WorkbenchSessionV1();
  const turn = session.begin("turn_1", "request_1", "Question", noContext, 100);
  assert.ok(turn);
  session.complete(turn.turnId, response("Answer"), 125);
  assert.equal(session.feedback(turn.turnId, "good")?.feedback, "good");
  assert.equal(session.feedback(turn.turnId, "good")?.feedback, null);
  assert.equal(session.find(turn.turnId)?.question, "Question");
  assert.equal(session.find(turn.turnId)?.completedAtMs, 125);
});

test("Workbench controller keeps one reusable editor panel", () => {
  const source = readFileSync("src/ui/workbench.ts", "utf8");
  assert.match(source, /if \(this\.panel\) \{ this\.panel\.reveal/);
  assert.match(source, /this\.panel\.onDidDispose/);
  assert.match(source, /createWebviewPanel/);
  assert.match(source, /postSession/);
  assert.match(source, /replaceSession/);
  assert.match(source, /this\.connection\.availability/);
  assert.doesNotMatch(source, /operationChanged/);
  assert.match(source, /clearInterval\(telemetry\)/);
});

test("Workbench renderer projects status and lifecycle from host-owned snapshots", () => {
  const script = workbenchRendererScript();
  assert.match(script, /active=session\.some\(turn=>turn\.status==='pending'\)/);
  assert.match(script, /connection\.system==='READY'/);
  assert.match(script, /Needs attention/);
  assert.match(script, /enforceAvailability/);
  assert.match(script, /connection\?\.availability/);
  assert.match(script, /sessionRevision=-1/);
  assert.match(script, /revision<sessionRevision/);
  assert.match(script, /renderSession\(data\.session,data\.sessionRevision\)/);
  assert.match(script, /turn\.status==='pending'/);
  assert.match(script, /resizeComposer/);
  assert.match(script, /question\.scrollHeight/);
  assert.match(script, /overflowY=scrollHeight>220\?'auto':'hidden'/);
  assert.doesNotMatch(script, /turnCompleted\|\|data\.type==='turnFailed'/);
  assert.match(script, /connection\.modelRole/);
  assert.match(script, /type:'selectModelRole'/);
});

test("Workbench Development mode is explicit and renders only advisory Codex task preparation", () => {
  const script = workbenchRendererScript();
  const host = readFileSync("src/ui/workbench.ts", "utf8");
  const manifest = JSON.parse(readFileSync("package.json", "utf8")) as { contributes: { configuration: { properties: Record<string, { default?: string; scope?: string }> } } };
  assert.deepEqual(parseWebviewMessage({ type: "selectWorkbenchMode" }), { type: "selectWorkbenchMode" });
  assert.deepEqual(parseWebviewMessage({ type: "selectDevelopmentIntent" }), { type: "selectDevelopmentIntent" });
  assert.deepEqual(parseWebviewMessage({ type: "prepareCodexTask", turnId: "turn_1" }), { type: "prepareCodexTask", turnId: "turn_1" });
  assert.equal(manifest.contributes.configuration.properties["chappie.workbenchMode"].default, "CHAT");
  assert.equal(manifest.contributes.configuration.properties["chappie.workbenchMode"].scope, "resource");
  assert.match(host, /turn\.mode === "DEVELOPMENT"/);
  assert.match(host, /analyzeDevelopment/);
  assert.match(host, /turn\.requestedIntent!/);
  assert.match(host, /intentSelection\.reset\(\)/);
  assert.match(script, /Intent: '/);
  assert.match(script, /connection\.mode!==\'DEVELOPMENT\'/);
  assert.match(script, /type:'selectDevelopmentIntent'/);
  assert.match(host, /prepareCodexTask/);
  assert.match(host, /task\.state !== "ADVISORY"/);
  assert.match(host, /turn\.development\.readiness !== "READY_FOR_TASK"/);
  assert.match(script, /Summary/);
  assert.match(script, /Prepare Codex Handoff/);
  assert.match(script, /presentation\.allowHandoff/);
  assert.match(script, /turn\.codexTask\.approval_status/);
  assert.doesNotMatch(host, /child_process\.exec|Codex invocation/);
});

test("Workbench presents bounded Development outcomes separately from technical diagnostics", () => {
  const script = workbenchRendererScript();
  const host = readFileSync("src/ui/workbench.ts", "utf8");
  assert.match(script, /presentation\.summary/);
  assert.match(script, /presentation\.sections/);
  assert.match(script, /presentation\.nextAction/);
  assert.match(script, /Status: '\+presentation\.status/);
  assert.match(script, /developmentInspectorLines/);
  assert.match(script, /Request ID/);
  assert.match(script, /Failed stage/);
  assert.match(script, /Evidence digest/);
  assert.match(host, /turn\.development\.outcome !== undefined/);
  assert.match(host, /outcome\.status !== "NEEDS_HUMAN_REVIEW"/);
  assert.doesNotMatch(script, /JSON\.stringify\(turn\.development/);
  assert.doesNotMatch(script, /raw provider payload|chain.of.thought|Knowledge text/i);
});

test("Workbench Webview renderer remains executable with current controls", () => {
  const script = workbenchRendererScript();
  assert.doesNotThrow(() => new Function(script));
  assert.match(script, /connection\.modelRole/);
  assert.match(script, /connection\.mode/);
  assert.match(script, /let inspectorOpen=false/);
  assert.match(script, /developmentNode/);
  assert.match(script, /prepareCodexTask/);
  assert.match(script, /footer\.replaceChildren\(contextActions,footerSpacer,footerActions\)/);
});

test("Workbench development snapshots are detached and task projection requires a valid analysis", () => {
  const session = new WorkbenchSessionV1();
  const turn = session.begin("development_1", "request_1", "Plan an improvement", noContext, 100, [], "DEVELOPMENT");
  assert.ok(turn);
  const analysis: DevelopmentAnalysisV1 = {
    task_intent: null, answer: null, plan_items: [],
    identity: "chappie_development_analysis/2.0.0", state: "OK", request: "Plan an improvement", evidence: null,
    facts: ["fact"], inferences: [], unknowns: [], architecture_owners: [], affected_contracts: [], likely_files: [], risks: [], required_tests: [], non_goals: [], acceptance_criteria: [], confidence: 1, validation_status: "VALID", readiness: "READY_FOR_TASK", reason_code: null, context_map: null, current_evidence_handles: [], proposed_scope: null,
  };
  assert.ok(session.completeDevelopment(turn.turnId, analysis, 200));
  analysis.facts[0] = "outside mutation";
  assert.equal(session.find(turn.turnId)?.development?.facts[0], "fact");
  assert.equal(session.setCodexTask("missing", { identity: "chappie_codex_handoff/1.0.0" } as never), undefined);
});

test("typed Development product outcomes remain completed turns and conversation stays usable", () => {
  const session = new WorkbenchSessionV1();
  const turn = session.begin("development_failure", "request_failure", "Analyze safely", noContext, 100, [], "DEVELOPMENT");
  assert.ok(turn);
  const analysis: DevelopmentAnalysisV1 = {
    task_intent: null, answer: null, plan_items: [], identity: "chappie_development_analysis/2.0.0", state: "MODEL_UNAVAILABLE", request: "Analyze safely", evidence: null,
    facts: [], inferences: [], unknowns: [], architecture_owners: [], affected_contracts: [], likely_files: [], risks: [], required_tests: [], non_goals: [], acceptance_criteria: [], confidence: null,
    validation_status: "INVALID", readiness: null, reason_code: "MODEL_ROLE_UNAVAILABLE", context_map: null, current_evidence_handles: [], proposed_scope: null,
    outcome: { identity: "chappie_development_outcome/1.1.0", status: "MODEL_UNAVAILABLE", blocker_code: "MODEL_ROLE_UNAVAILABLE", explanation: "The selected model is unavailable.", required_context: null, suggested_next_action: "Retry when it is available.", safe_to_retry: true, request_id: "request_failure", failed_stage: "ROLE_RESOLUTION", reason_code: "MODEL_ROLE_UNAVAILABLE", no_changes_made: true, why_it_blocks: "No fallback is permitted." },
  };
  const completed = session.completeDevelopment(turn.turnId, analysis, 200);
  assert.equal(completed?.status, "complete");
  assert.equal(completed?.developmentPresentation?.status, "Model unavailable");
  assert.equal(completed?.developmentPresentation?.allowHandoff, false);
  assert.ok(session.begin("next", "next_request", "Continue conversation", noContext, 300, [], "DEVELOPMENT"));
});
