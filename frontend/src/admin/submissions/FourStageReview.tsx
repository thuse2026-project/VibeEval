import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../shared/api";

type Criterion = {
  key: string;
  label: string;
  maximum: number;
  answer_id: string;
  group: string;
  evidence_required?: boolean;
};
type Stage = {
  submission_id: string | null;
  title: string;
  score: number | null;
};
type Evidence = {
  id: string;
  kind: string;
  actor: string;
  data: Record<string, unknown>;
};
type SuggestedMark = {
  key?: string;
  criterion_id?: string;
  score: number | null;
  reason: string;
  quote?: string;
  answer_quote?: string;
  evidence?: unknown[];
};
type Proposal = {
  marks?: SuggestedMark[];
  questions?: { question_id: string; marks: SuggestedMark[] }[];
  review_note?: string;
};
type Material = {
  metadata: { title: string; candidate_name: string; kind: string };
  revision: string;
  questions: { id: string; title: string; body: string }[];
  answers: Record<string, unknown>;
  rubric: Criterion[];
  basis: {
    content_sha256: string;
    code_sha256?: string;
    evidence?: { completeness: string; reason?: string };
  };
  files: { path: string; content: string }[];
  components: {
    objective?: {
      score: number | null;
      status: string;
      cap?: number | null;
      groups?: { name: string; core?: boolean; passed: boolean }[];
    };
    answers: {
      status: string;
      reason?: string;
      attempts?: { failure?: string | null }[];
      suggestion?: Proposal | null;
    };
    examiner_review?: {
      scores: Record<string, number | null>;
      evidence_notes?: Record<string, string>;
      evidence_refs?: Record<string, string>;
      reason: string;
      gap_reviewed: boolean;
      raw: number | null;
      final: number | null;
    };
  };
  exam: {
    stages: Stage[];
    revision: string;
    ready: boolean;
    publication: { examiner: string; published_at: number } | null;
  };
};
type EvidencePage = { items: Evidence[]; next_cursor: number | null };
const pretty = (v: unknown) =>
  typeof v === "string" ? v : JSON.stringify(v, null, 2);
const base = "/api/v1/admin/r2/submissions";
const failureLabels: Record<string, string> = {
  MODEL_UNAVAILABLE: "模型服务不可用",
  INVALID_RESPONSE_FORMAT: "模型回复格式未通过校验",
  GRADING_BINDING_CHANGED: "评分材料或模型配置已变更",
  INPUT_EXCEEDS_BUDGET: "材料超过模型输入上限",
  MISSING_MODEL_CONTENT: "模型未返回评分内容",
};

export default function FourStageReview({
  sid,
  onExpired,
}: {
  sid: string;
  onExpired: () => void;
}) {
  const [data, setData] = useState<Material | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [scores, setScores] = useState<Record<string, string>>({}),
    [pending, setPending] = useState<Record<string, boolean>>({}),
    [notes, setNotes] = useState<Record<string, string>>({}),
    [refs, setRefs] = useState<Record<string, string>>({}),
    [reason, setReason] = useState(""),
    [ack, setAck] = useState(false);
  const [items, setItems] = useState<Evidence[]>([]),
    [cursor, setCursor] = useState<number | null>(0),
    [file, setFile] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [dirty, setDirty] = useState(false);
  const path = sid + "/four-stage";
  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) onExpired();
      else setError(e instanceof Error ? e.message : "读取失败");
    },
    [onExpired],
  );
  const reload = useCallback(async () => {
    const m = await api<Material>(path + "/materials", {}, base, 90000);
    setData(m);
    const r = m.components.examiner_review;
    setScores(
      Object.fromEntries(
        m.rubric.map((c) => [c.key, r?.scores[c.key]?.toString() ?? ""]),
      ),
    );
    setReason(r?.reason ?? "");
    setPending(
      Object.fromEntries(
        m.rubric.map((c) => [c.key, r?.scores[c.key] === null]),
      ),
    );
    setNotes(r?.evidence_notes ?? {});
    setRefs(r?.evidence_refs ?? {});
    setAck(r?.gap_reviewed ?? false);
    setConfirm(false);
    setDirty(false);
  }, [path]);
  useEffect(() => {
    void reload().catch(fail);
  }, [reload, fail]);
  async function action(url: string, body: unknown) {
    setBusy(true);
    setError("");
    try {
      await api(url, body, base, 180000);
      await reload();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  async function more() {
    if (cursor === null) return;
    setBusy(true);
    try {
      const v = await api<EvidencePage>(
        path + "/evidence?after=" + cursor,
        undefined,
        base,
      );
      setItems((old) => [...old, ...v.items]);
      setCursor(v.next_cursor);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }
  const suggestion = data?.components.answers.suggestion;
  const marks =
    suggestion?.marks ??
    suggestion?.questions?.flatMap((q) =>
      q.marks.map((m) => ({ ...m, key: q.question_id + "." + m.criterion_id })),
    ) ??
    [];
  const locked = !!data?.exam.publication || busy;
  const objective = data?.components.objective;
  const review = data?.components.examiner_review;
  const partial = data?.basis.evidence?.completeness === "partial";
  return (
    <main className="r2-review">
      <a href="/admin/r2/submissions">返回答卷列表</a>
      {error && <p role="alert">{error}</p>}
      {!data ? (
        <p>正在读取封存材料…</p>
      ) : (
        <>
          <h1>
            {data.metadata.candidate_name} · {data.metadata.title}
          </h1>
          <nav className="r2-review-actions">
            {data.exam.stages.map((s) =>
              s.submission_id ? (
                <a key={s.title} href={"/admin/r2/four/" + s.submission_id}>
                  {s.title} · {s.score ?? "待评"}
                </a>
              ) : (
                <span key={s.title}>{s.title} · 待交卷</span>
              ),
            )}
          </nav>
          <section>
            <h2>封存答卷</h2>
            {data.questions.map((q) => (
              <details key={q.id}>
                <summary>{q.title}</summary>
                <p>{q.body}</p>
                <pre>{pretty(data.answers[q.id]) || "未填写"}</pre>
              </details>
            ))}
          </section>
          {data.basis.evidence && (
            <section>
              <h2>编程证据</h2>
              <p>
                代码摘要 <code>{data.basis.code_sha256}</code>
              </p>
              <p>
                {data.basis.evidence.completeness === "complete"
                  ? "已采集"
                  : "采集不完整 · " + data.basis.evidence.reason}
              </p>
              <label>
                查看冻结文件{" "}
                <select value={file} onChange={(e) => setFile(e.target.value)}>
                  <option value="">选择文件</option>
                  {data.files.map((f) => (
                    <option key={f.path}>{f.path}</option>
                  ))}
                </select>
              </label>
              {file && (
                <pre>{data.files.find((f) => f.path === file)?.content}</pre>
              )}
              {items.map((e) => (
                <details key={e.id}>
                  <summary>
                    {e.kind} · {e.actor} · {e.id}
                  </summary>
                  <pre>{pretty(e.data)}</pre>
                </details>
              ))}
              {cursor !== null && (
                <button disabled={busy} onClick={() => void more()}>
                  {items.length ? "下一页操作记录" : "读取操作记录"}
                </button>
              )}
            </section>
          )}
          <section>
            <h2>评分依据</h2>
            {data.components.objective && (
              <p>
                客观评测 {data.components.objective.score ?? "待评"} / 70{" "}
                <button
                  disabled={locked}
                  onClick={() => void action(sid + "/evaluate", {})}
                >
                  运行客观评测
                </button>
              </p>
            )}
            {objective?.cap != null && (
              <p>
                本题上限 {objective.cap} 分。未通过的核心测试：
                {objective.groups
                  ?.filter((g) => g.core && !g.passed)
                  .map((g) => g.name)
                  .join("、") || "核心修复验证"}
                。
              </p>
            )}
            {review && (
              <p>
                {review.final === null ? (
                  "仍有分项待复核，整场暂不能发布。"
                ) : (
                  <>
                    原始合计 {review.raw} 分 · 本题最终 {review.final} 分
                  </>
                )}
              </p>
            )}
            <p>
              问答建议 ·{" "}
              {(
                {
                  pending: "待评分",
                  retryable: "可重试一次",
                  suggested: "已生成",
                  needs_review: "需人工复核",
                } as Record<string, string>
              )[data.components.answers.status] ??
                data.components.answers.status}
              <button
                disabled={
                  locked ||
                  !!data.components.examiner_review ||
                  !["pending", "retryable"].includes(
                    data.components.answers.status,
                  )
                }
                onClick={() => void action(path + "/suggest", {})}
              >
                生成评分建议
              </button>
            </p>
            {!!data.components.answers.attempts?.length && (
              <details open={data.components.answers.status === "needs_review"}>
                <summary>
                  模型评分记录（{data.components.answers.attempts.length} 次）
                </summary>
                <ol>
                  {data.components.answers.attempts.map((a, i) => (
                    <li key={i}>
                      {a.failure
                        ? (failureLabels[a.failure] ?? a.failure)
                        : "已返回分项建议"}
                    </li>
                  ))}
                </ol>
              </details>
            )}
            {data.components.answers.reason && (
              <p>
                {failureLabels[data.components.answers.reason] ??
                  data.components.answers.reason}
              </p>
            )}
            {suggestion && (
              <details>
                <summary>分项建议、理由与引用</summary>
                {suggestion.review_note && <p>{suggestion.review_note}</p>}
                {marks.map((m) => (
                  <div key={m.key}>
                    <strong>
                      {m.key} · {m.score ?? "需复核"}
                    </strong>
                    <p>{m.reason}</p>
                    <blockquote>{m.quote ?? m.answer_quote}</blockquote>
                    {!!m.evidence?.length && <pre>{pretty(m.evidence)}</pre>}
                  </div>
                ))}
                <button
                  disabled={locked}
                  onClick={() => {
                    setDirty(true);
                    setConfirm(false);
                    setScores((old) => ({
                      ...old,
                      ...Object.fromEntries(
                        marks
                          .filter((m) => m.key && m.score != null)
                          .map((m) => [m.key, String(m.score)]),
                      ),
                    }));
                  }}
                >
                  将建议分填入本题草稿
                </button>
              </details>
            )}
          </section>
          <form
            onChange={() => {
              setDirty(true);
              setConfirm(false);
            }}
            onSubmit={(e) => {
              e.preventDefault();
              void action(path + "/review", {
                revision: data.revision,
                scores: Object.fromEntries(
                  Object.entries(scores).map(([k, v]) => [
                    k,
                    pending[k] || !v.trim() ? null : Number(v),
                  ]),
                ),
                evidence_notes: Object.fromEntries(
                  Object.entries(notes).filter(([, v]) => v.trim()),
                ),
                evidence_refs: Object.fromEntries(
                  Object.entries(refs).filter(([k, v]) => !pending[k] && v),
                ),
                reason,
                gap_reviewed: ack,
              });
            }}
          >
            <h2>考官评定</h2>
            <fieldset disabled={locked}>
              {data.rubric.map((c) => (
                <div key={c.key} style={{ display: "block", margin: "10px 0" }}>
                  <label>
                    {c.key} · {c.label}{" "}
                    <input
                      aria-label={c.key}
                      disabled={!!pending[c.key]}
                      required={!pending[c.key]}
                      type="number"
                      step="0.01"
                      min="0"
                      max={c.maximum}
                      value={scores[c.key] ?? ""}
                      onChange={(e) =>
                        setScores({ ...scores, [c.key]: e.target.value })
                      }
                    />{" "}
                    / {c.maximum}
                  </label>{" "}
                  <label>
                    <input
                      type="checkbox"
                      checked={!!pending[c.key]}
                      onChange={(e) =>
                        setPending({ ...pending, [c.key]: e.target.checked })
                      }
                    />{" "}
                    待复核
                  </label>
                  {partial && c.evidence_required && !pending[c.key] && (
                    <label>
                      依据来源
                      <select
                        required
                        value={refs[c.key] ?? ""}
                        onChange={(e) =>
                          setRefs({ ...refs, [c.key]: e.target.value })
                        }
                      >
                        <option value="">选择冻结文件或已读取的操作记录</option>
                        {refs[c.key]?.startsWith("event:") &&
                          !items.some(
                            (i) => "event:" + i.id === refs[c.key],
                          ) && (
                            <option value={refs[c.key]}>{refs[c.key]}</option>
                          )}
                        {items.map((i) => (
                          <option key={i.id} value={"event:" + i.id}>
                            {i.kind} · {i.id}
                          </option>
                        ))}
                        {data.files.map((f) => (
                          <option key={f.path} value={"file:" + f.path}>
                            {f.path}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {(pending[c.key] ||
                    (partial && c.evidence_required) ||
                    notes[c.key]) && (
                    <label>
                      {pending[c.key] ? "缺少的依据" : "本项采用的依据"}
                      <textarea
                        required={
                          !!pending[c.key] || !!(partial && c.evidence_required)
                        }
                        maxLength={2000}
                        value={notes[c.key] ?? ""}
                        onChange={(e) =>
                          setNotes({ ...notes, [c.key]: e.target.value })
                        }
                      />
                    </label>
                  )}
                </div>
              ))}
              <label>
                评定依据及调整原因{" "}
                <textarea
                  required
                  maxLength={4000}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              {data.basis.evidence?.completeness === "partial" && (
                <label>
                  <input
                    type="checkbox"
                    required
                    checked={ack}
                    onChange={(e) => setAck(e.target.checked)}
                  />
                  已核实采集缺口及可替代依据
                </label>
              )}
              <button>保存本题评定</button>
            </fieldset>
          </form>
          <section>
            <h2>整场发布</h2>
            {data.exam.publication ? (
              <p>已由 {data.exam.publication.examiner} 确认发布</p>
            ) : confirm ? (
              <>
                <p>将锁定上方四份评定。考生端继续显示离场页。</p>
                <button
                  disabled={busy}
                  onClick={() =>
                    void action(path + "/publish", {
                      revision: data.exam.revision,
                      confirm: true,
                    })
                  }
                >
                  确认发布四题成绩
                </button>{" "}
                <button onClick={() => setConfirm(false)}>返回核对</button>
              </>
            ) : (
              <button
                disabled={busy || dirty || !data.exam.ready}
                onClick={() => setConfirm(true)}
              >
                核对并发布
              </button>
            )}
          </section>
        </>
      )}
    </main>
  );
}
