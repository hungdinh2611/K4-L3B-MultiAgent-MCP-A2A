# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Workflow là state machine Python async thuần, dùng luật cố định (không LLM). Mỗi case chạy trong một `CaseContext` riêng ([a2a.py](src/student_agent/a2a.py)) giữ MCP access, evidence ledger, cache và trace của đúng case đó.

```text
case input
   │ case_received (cli)
   ▼
Coordinator ──task_assigned──► Entity agent ──(get_order, get_customer_history)
   │                               │ handoff: order_id + episode scope + customer context
   │ task_assigned                 ▼
   ├──► Order/product agent ──(get_order_items; get_product_context khi unavailable; get_sellers fallback)
   │                               │ handoff: items, sellers, order value
   │ task_assigned (song song)     ▼
   ├──► Shipment agent ───────(get_shipment_summary)
   └──► Payment/refund agent ─(get_payment_timeline; get_refund_timeline khi claim về refund; get_order_payments fallback)
                                   │ handoff: findings về coordinator
   ▼
Coordinator ──task_assigned──► Conflict resolver ──handoff──► Policy agent ──(get_policy)
                                                                │ policy_decided
                                                                ▼ handoff
                                                             Verifier ── verification_completed
                                                                │ handoff: finalize + output
                                                                ▼
                                                  Coordinator → outputs/<case_id>.json
                                                                │ case_finalized (cli)
```

Mọi MCP call đi qua `CaseContext.fetch`, nơi kiểm tra quyền, cache và retry, rồi emit `tool_result_consumed`. Mọi message đi qua `CaseContext.dispatch` / `route`, nơi kiểm tra scope và emit `task_assigned` / `handoff`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
<<<<<<< HEAD
| Entity/customer (`entity-agent`) | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint` | Xếp hạng và xác minh candidate, loại candidate sai, lấy lịch sử khách hàng | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context`, order row → coordinator |
| Coordinator (`coordinator`) | Case input, handoff của specialist | Giao task, fan-out specialist song song, mở review chain, ghi output | không có | `task_assigned`; nhận `finalize` từ verifier |
| Order/product (`order-agent`) | `order_id` đã resolve | Item, seller, sản phẩm liên quan | `get_order_items`, `get_product_context`, `get_sellers` | `item_ids`, `seller_ids`, giá item → coordinator |
| Shipment (`shipment-agent`) | `order_id` | Timeline giao hàng, seller handoff limit, trễ do seller hay logistics | `get_shipment_summary` | `shipment_analysis`, `shipment_ids` → coordinator |
| Payment/refund (`payment-agent`) | `order_id` | Đối soát capture, lifecycle thanh toán, refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis`, `payment_references` → coordinator |
| Policy (`policy-agent`) | Findings + conflicts, `policy_version` | Áp policy: primary issue, trách nhiệm, số tiền hoàn, actions | `get_policy` | `policy_decided`; draft decision → verifier |
| Conflict resolver (`conflict-agent`) | Findings của mọi specialist | Phát hiện nguồn mâu thuẫn, chọn nguồn authoritative | không có | `data_conflicts` → policy |
| Verifier (`verifier-agent`) | Draft decision + evidence ledger | Kiểm tra invariant, hiệu chỉnh confidence, lắp output | không có | `verification_completed`; `finalize` + output → coordinator |

Áp dụng least privilege: mỗi tool thuộc đúng một actor (có test `test_every_tool_is_owned_by_exactly_one_agent`). Gọi tool ngoài allowlist sẽ ném `ToolPermissionError` trước khi có MCP call.
=======
| `coordinator` | Case file | Route once, own the verified order scope | none | Scoped case to each specialist |
| `entity-agent` | Claimed and candidate identifiers, customer hint | Confirm exactly one order; reject every other candidate | `get_order`, `get_customer_history` | Verified `OrderScope` to coordinator |
| `order-agent` | Verified order | Item and seller membership, order value | `get_order_items`, `get_product_context` | Item facts with evidence refs |
| `shipment-agent` | Verified order, confirmed items | Compare promised, handed-over and delivered timestamps; read the seller record where one is answerable | `get_shipment_summary`, `get_sellers` | Timeline verdict with evidence refs |
| `payment-agent` | Verified order, order value | Reconcile captures, track the refund lifecycle | `get_payment_timeline`, `get_refund_timeline` | Monetary findings with evidence refs |
| `conflict-agent` | Contradictory sourced values | Name both sources and the precedence rule applied | none | `data_conflicts` entries |
| `policy-agent` | Scoped findings, `policy_version` | Read the published rule table, decide status, action and amount | `get_policy` | Supported recommendation |
| `verifier-agent` | Proposed L3B object, evidence ledger | Reconciliation, scope, ownership and chronology checks | none | Validated output or a rejection |

`solve_case(case, gateway, trace)` in `src/student_agent/workflow.py` runs this sequence. The code is split three ways: `workflow.py` holds the coordinator and nothing else, `a2a.py` holds the protocol — `A2AMessage`, the `CaseContext` and the routing — and `agents.py` holds the specialists with the domain reasoning they own. The runner in `cli.py` emits `case_received` before the call and `case_finalized` after it, so the workflow itself never duplicates those two events.

## Agent-to-agent protocol

No agent holds the gateway, the trace, or another agent. Each is handed the `CaseContext` and answers with one `A2AMessage` naming its sender, recipient, intent and the references it stands behind. Routing a message through the context is the same act as recording it, which is what makes the trace an audit of the run rather than a commentary on it:

- **An assignment is checked, not trusted.** A specialist holds the order, episode and window it was handed against the scope the entity agent actually proved, and raises `ProtocolViolation` on a mismatch, so a mis-routed task cannot quietly widen the scope.
- **A cited reference must be owned.** `CaseContext` refuses any reply quoting an `evidence_ref` this case never consumed, so a fabricated reference cannot reach the output by way of a handoff.
- **The review chain is a chain.** `conflict-agent` refers to `policy-agent`, which refers to `verifier-agent`, which alone may answer `finalize`. A referral back to its own sender, or a chain that never finalises, is a violation rather than a silent result.
- **Parallel specialists stay honest.** `shipment-agent` and `payment-agent` run concurrently. A payment cannot decide whether a refund still matters until the shipment verdict is in, so the payment agent waits for that announcement at the one point it needs it — and only when the refund could still change the outcome.
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e

## 3. Entity resolution và A2A protocol

<<<<<<< HEAD
**Entity resolution** (`entity-agent`):

1. Xếp hạng candidate: `claimed_order_id` trước, sau đó các candidate khác theo thứ tự input. Chỉ ID có định dạng order hợp lệ (32 ký tự hex) mới được tra cứu. Placeholder như `candidate-NNN` bị loại mà không tốn call.
2. `get_order` trên candidate đầu tiên. Nếu tồn tại và khớp scope của case thì `resolved`, các candidate còn lại vào `rejected_candidates`.
3. Nếu không tìm thấy thì thử candidate tiếp theo, tối đa 2 lần tra `get_order`. Không có candidate nào khớp thì `not_found`. Nhiều candidate cùng khớp mà không phân biệt được thì `ambiguous`.
4. Order row không có `customer_unique_id`, nên `get_customer_history` được gọi với `customer_unique_id_hint`. Hint chỉ được chấp nhận khi history chứa đúng order đã resolve với cùng `customer_id`. Nếu không khớp thì `customer_unique_id: null`.
5. **Episode scoping**: cùng một order ID có thể mang nhiều episode (các row có ngày mua khác nhau). `get_order` trả về episode đầu tiên, và đó không nhất thiết là episode của case. Episode của case được chọn trong các episode có `order_purchase_timestamp` ≤ `opened_at` ([timeline.py](src/student_agent/timeline.py)): ưu tiên episode muộn nhất mà order row khớp claim (`canceled` / `unavailable` theo status, trễ giao hoặc giao đúng hạn theo mốc thời gian), nếu không có thì lấy episode muộn nhất. Claim về payment/refund không nhận ra được từ order row nên dùng episode muộn nhất. Event, item row (theo `shipping_limit_date`), capture và refund được gán cho episode theo cửa sổ nửa mở `[purchase của episode, purchase của episode kế tiếp)`. Mọi specialist chỉ phân tích dữ liệu trong cửa sổ này; episode còn lại là nhiễu và được ghi vào `data_conflicts`.
6. Confidence: `resolved` và history xác minh được thì 0.95; `resolved` không có history thì 0.8; `ambiguous` 0.4; `not_found` 0.1. Khi không `resolved`, các specialist theo order không chạy và case chuyển `needs_investigation`.

Thứ tự handoff: entity → order → (shipment ∥ payment). Payment cần `order_value_brl` từ order-agent để phân biệt split payment (tổng capture = giá trị đơn) với duplicate capture (các capture trùng số tiền nhưng tổng lệch giá trị đơn).
=======
`CaseContext` is the only door to the gateway, so all five gateway rules hold in one place:

1. **`case_id` on every call.** `CaseContext.fetch` supplies it from the case under investigation; a specialist cannot omit or override it. The server refuses an identifier belonging to another case, which is the intended behaviour and is treated as "no evidence", never worked around.
2. **References are never synthesised.** `evidence_ref` is copied verbatim out of the envelope into `EvidenceLedger`. The ledger is created per `solve_case` call, so it cannot hold a reference from another case, and the output can only cite what the ledger holds.
3. **Only supporting evidence is cited.** Each tool answers a field the output actually carries. `get_order_payments` is skipped because `get_payment_timeline` returns the same payment rows plus the lifecycle events. `get_sellers` is requested only where a seller is actually held answerable — a `seller_delay` verdict or an unavailable order — because that is where `responsible_parties` and `late_seller_ids` name one; elsewhere the seller record would support nothing.
4. **`tool_result_consumed` after validation only.** The event is emitted once the envelope has passed `mcp-evidence-response-v1` in `EvidenceGateway.call` and the tool's declared domain matches `TOOL_DOMAINS`. A wrong-domain envelope is discarded with no event and no ledger entry.
5. **The client trace mirrors the server audit.** Every consumed envelope produces exactly one event naming the actor, the tool and the single reference, so the client sequence can be reconciled against the server's independent hash, latency and status audit.

Per case the run spends seven calls — `get_order`, `get_customer_history`, `get_order_items`, `get_product_context`, `get_shipment_summary`, `get_payment_timeline`, `get_policy` — plus `get_refund_timeline` only where the case can still turn on a refund. A canceled or unavailable order, an open reconciliation mismatch or a confirmed late delivery already outranks any refund state, so asking there would spend an audited call on evidence that cannot change the outcome. A tool-level refusal is a final answer and is not retried; only a timeout or transport fault gets a second attempt, capped at two per argument set. Requests are read only and idempotent.
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e

**A2A message envelope** (`A2AMessage`): `case_id`, `sender`, `recipient`, `intent`, `payload`, `evidence_refs`. `CaseContext.dispatch` từ chối message nếu:

- `case_id` khác case hiện tại (correlation theo `case_id`, chống dùng chéo case);
- giao sai `recipient`, hoặc agent trả message với `sender` không phải chính nó;
- message trích dẫn `evidence_ref` không có trong ledger của case.

**Handoff**: message từ coordinator emit `task_assigned`; mọi reply emit `handoff` (actor → target, `decision_code` = intent, kèm evidence refs). Review chain là tuyến tính conflict → policy → verifier → coordinator; `route` giới hạn 8 hop nên không thể lặp vô hạn. Trace chỉ ghi sự kiện quan sát được, không ghi nội dung suy luận.

<<<<<<< HEAD
## 4. Evidence và conflict lifecycle
=======
The gateway holds more than one revision under the same `order_id`: `get_order` answers with exactly one of them, and `get_customer_history` returns them all. The one `get_order` returns is often purchased *after* the case was opened, and no complaint can be about an order placed later, so it cannot be taken as the subject on its own.

**The case intake timestamp settles it.** The subject is the newest revision already placed at `opened_at`; where none precedes intake, the earliest is taken. That revision's own row supplies `order_status` and the whole delivery timeline, and every downstream row is then attributed to it:
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e

1. **Validate**: `EvidenceGateway.call` validate mọi response theo `mcp-evidence-response-v1.schema.json`. Response sai contract thành `ToolFailure(invalid_response)` và không được dùng.
2. **Lưu**: `CaseContext.evidence` là ledger `evidence_ref → Evidence(tool_name, domain, data, warnings)`. `evidence_ref` luôn lấy nguyên văn từ server, không bao giờ tự sinh hay sửa. Context mới cho mỗi case nên evidence không thể tái sử dụng giữa các case.
3. **Trace**: `tool_result_consumed` được emit ngay khi actor nhận evidence, một lần cho mỗi cặp (actor, ref), kèm `tool_name` và `domain`.
4. **Chọn nguồn**:
   - Timeline của episode lấy từ row trong `get_customer_history`. Row của `get_order` và các trường top-level của `get_shipment_summary` phản ánh episode khác thì ghi conflict `order_timeline` / `shipment_timeline` với `selected_source: get_customer_history`, `resolution_code: CASE_TIMELINE_SCOPE`.
   - Capture, mismatch và refund lấy từ lifecycle event (`get_payment_timeline`, `get_refund_timeline`) trong cửa sổ episode, không lấy từ payment row vì payment row không có timestamp.
   - Row trùng hệt nhau (history, item, event) là episode bị nhân đôi, chỉ tính một lần.
   - **Bản ghi order bị nhân đôi** (history có row trùng hệt nhau): ghi conflict `order_record` (`DUPLICATE_RECORD_MERGED`) và kiểm chứng chéo bằng mọi nguồn độc lập: `get_order_payments`, `get_sellers`, `get_product_context`, `get_shipment_summary`, `get_refund_timeline`. Tất cả evidence có dữ liệu đều được trích dẫn. Refund của capture lạ (bị loại theo payment set) không được tính vào episode của case.
   - Payment row được gom thành payment set (mỗi lần `payment_sequential` quay về `1`). Nếu một set trả đúng giá trị đơn thì capture mang số tiền lạ trong cùng cửa sổ thuộc tình huống khác và bị loại: ghi conflict `captured_payments`, `selected_source: get_payment_timeline.payments`, `resolution_code: ORDER_VALUE_PAYMENT_SET`. Capture lặp lại đúng số tiền của set đó vẫn được giữ vì có thể là duplicate charge thật.
   - Shipment verdict lấy từ mốc thời gian của episode (carrier so với `shipping_limit_at`, giao hàng so với dự kiến). Event `delivered_late` dùng để đối chứng; nếu actor của event mâu thuẫn với mốc thời gian thì verdict là `conflicting`.
   - `get_refund_timeline` trả tool error khi order không có refund. Trường hợp này được coi là không có refund event (refunded = 0), không phải thiếu evidence.
   - `affected_entities.shipment_ids` và `payment_references` để rỗng vì evidence không có định danh shipment/payment. Không tự tạo ID.
5. **Conflict chưa giải quyết**: ghi vào `data_conflicts` với `selected_source: null` và `resolution_code` tương ứng, case chuyển `needs_investigation`.
6. **Map vào output**: mỗi specialist trả `evidence: {domain: ref}`. Output `evidence_refs` gồm domain nền (`order`, `customer`, `item`, `shipment`, `payment`, `policy`), vì mỗi domain này hỗ trợ một phần có mặt ở mọi output (entity, affected entities, shipment analysis, payment analysis, policy decision), các domain liên quan tới primary issue (bảng `RELEVANT_DOMAINS` trong [policy.py](src/student_agent/agents/policy.py)) và mọi ref mà `claim_assessments` trích dẫn. Ref đã gọi nhưng không hỗ trợ kết luận thì không được đưa vào; ví dụ refund timeline chỉ chứa refund của episode khác thì không được trích dẫn.

### Policy decision

`policy-agent` gọi `get_policy(policy_version)` và quyết định như sau:

1. **Tín hiệu trong episode** (thứ tự ưu tiên): order `canceled` / `unavailable` còn tiền chưa hoàn → `canceled_order_paid` / `unavailable_order_paid`; payment verdict `refund_failed` / `refund_pending` / `capture_mismatch` / `duplicate_capture` → issue tương ứng; shipment `seller_delay` / `logistics_delay` → `late_delivery_*`; split payment hợp lệ → `valid_split_payment`.
2. **Primary issue**: entity không `resolved` thì `insufficient_evidence`. Nếu topic khách claim có trong tín hiệu thì chọn topic đó; nếu không thì chọn tín hiệu ưu tiên cao nhất. Không có tín hiệu nào thì `unsupported_claim`; shipment `conflicting` hoặc thiếu cả shipment lẫn payment thì `insufficient_evidence`. Evidence thắng claim: claim sai thì claim bị đánh `unsupported`.
3. **Áp policy**: `case_status`, `recommended_action` và party type lấy từ rule của primary issue. `party_id` của seller lấy từ seller của case (policy chứa seller ID của order khác), các party khác để `null`.
4. **Số tiền hoàn** tính từ evidence rồi cap theo `refundable_total_brl`: order bị hủy / không có hàng → refundable; trễ giao → freight; mismatch → số tiền mismatch; duplicate → số tiền bị trùng; refund failed → số tiền refund thất bại. Rule có `refund_brl = 0` thì hoàn 0. Kết quả khác `refund_brl` của policy thì giảm confidence.
5. **Claim**: claim theo topic là `supported` khi topic trùng primary issue (trừ `valid_split_payment` / `unsupported_claim` vì đó là trường hợp khách không bị thiệt), ngược lại là `unsupported`. `requested_full_refund` là `supported` khi hoàn đủ refundable, `partially_supported` khi hoàn một phần, `insufficient_evidence` khi case cần điều tra thêm, `unsupported` khi không hoàn.
6. **Confidence**: 0.9 khi evidence khớp claim; 0.7 khi evidence bác claim; 0.35 khi `insufficient_evidence`. Trừ thêm khi có nhiều tín hiệu, có conflict chưa giải quyết, thiếu policy hoặc số tiền lệch policy. Giới hạn trong [0.05, 0.95]. Emit `policy_decided` với `decision_code` = primary issue.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / lỗi transport | 1 retry (tối đa 2 attempt, 60 s/attempt) | Domain đó thành `insufficient_evidence` | `ToolFailure(transient_exhausted)`; handoff của specialist mang findings thiếu domain |
| Tool error (not found, 403) | 0 (lỗi xác định, retry vô ích) | Như trên | `ToolFailure(tool_error)` |
| Entity not found/ambiguous | Tối đa 2 lần tra `get_order` | `entity_resolution.status` = `not_found`/`ambiguous`, bỏ qua specialist theo order, `needs_investigation` | handoff `resolve_entity` → coordinator |
| Source conflict | 0 (không gọi thêm tool) | Chọn nguồn theo precedence; không chọn được thì `selected_source: null` | handoff `conflicts_resolved`, `data_conflicts` |
| Invalid specialist result | 0 | Lỗi lập trình: fail-fast bằng `ProtocolViolation` thay vì bịa output | exception khi `day09 run` |
| Hết call budget | — | Call bị chặn trước khi gửi, domain thành `insufficient_evidence` | `ToolFailure(budget_exhausted)` |

**Query budget**: tối đa 12 MCP call mỗi case; thực tế 6–7, trung bình khoảng 6.3. Luôn gọi 6 tool mà evidence được trích dẫn ở mọi output: `get_order`, `get_customer_history`, `get_order_items`, `get_shipment_summary`, `get_payment_timeline`, `get_policy`. `get_refund_timeline` chỉ gọi khi claim là `refund_pending` / `refund_failed`: với order khác tool không có dữ liệu (tool error) hoặc chỉ có refund của episode khác. `get_product_context` chỉ gọi khi episode `unavailable` hoặc claim `unavailable_order_paid`, vì đó là trường hợp duy nhất product evidence hỗ trợ kết luận. `get_order_payments` (trùng `payments` trong payment timeline) và `get_sellers` (trùng `seller_id` trong item row) chỉ gọi khi nguồn chính thất bại. Candidate sai định dạng bị loại mà không gọi tool. Cache theo `(tool, arguments)` trong phạm vi case và gộp các call trùng đang chạy đồng thời, nên agent khác nhau hỏi cùng dữ liệu chỉ tốn 1 call. Retry chỉ áp dụng cho read idempotent và không bao giờ biến evidence thiếu thành dữ liệu phỏng đoán.

## 6. Verification invariants

`verifier-agent` ([verifier.py](src/student_agent/agents/verifier.py)) lắp output từ findings và decision, chạy từng check dưới đây. Check nào phát hiện vi phạm thì sửa tại chỗ và ghi correction code. Sau đó verifier validate JSON Schema và emit `verification_completed` (`decision_code` = `passed` / `corrected`, attributes gồm số check, số correction và các correction code).

| Check | Invariant | Correction code |
| --- | --- | --- |
<<<<<<< HEAD
| Entity scope | `resolved_order_ids` ⊆ candidate; không giao `rejected_candidates`; `affected_entities.order_ids` = order đã resolve | `ENTITY_OUT_OF_SCOPE`, `REJECTED_OVERLAP`, `AFFECTED_ORDER_MISMATCH` |
| Evidence ownership / claim linkage | Mọi ref trong output có trong ledger của case **và** đã có `tool_result_consumed`; ref của claim ⊆ `evidence_refs` | `UNTRACED_EVIDENCE` |
| Payment/refund totals | refundable = captured − refunded ≥ 0; refund ≤ refundable; refund = tổng `refund_lines` | `REFUNDABLE_RECOMPUTED`, `REFUND_CAPPED`, `REFUND_LINES_TOTAL` |
| Status/action | Action không trùng; `no_action` thì refund 0, không có refund line, action `document_no_action` | `DUPLICATE_ACTIONS`, `NO_ACTION_WITH_REFUND` |
| Responsibility | `late_seller_ids` ⊆ `seller_ids` và chỉ có khi `seller_delay`; party seller ∈ `seller_ids`; party khác không mang ID ngoài case | `LATE_SELLER_SCOPE`, `SELLER_PARTY_SCOPE`, `FOREIGN_PARTY_ID` |
| Schema | Output đúng `l3b-output-v2.schema.json` | exception (lỗi lập trình, không bịa output) |
=======
| Order revision | Newest revision placed at or before `opened_at` | `REVISION_OPEN_AT_CASE_INTAKE` |
| Item, shipping limit | `shipping_limit_date` within 21 days after the subject's purchase | `ITEM_SCOPED_TO_PURCHASE_WINDOW` |
| Capture, reconciliation event | Within 6 hours of the subject's `order_approved_at` | `CAPTURE_SCOPED_TO_ORDER_APPROVED_AT` |
| Refund event | Repays one in-scope capture, within 45 days of the subject's delivery | `CAPTURE_SCOPED_TO_ORDER_APPROVED_AT` |
| Shipment event | Within one day of the subject's `delivered_customer_at` | `EVENT_OUTSIDE_AUTHORITATIVE_DELIVERY` |

`get_shipment_summary` answers for the whole `order_id`, so its delivery timestamps describe whichever revision shipped. Once a revision is scoped, that revision owns its timeline and the summary cannot override it — otherwise a canceled revision that never shipped would inherit the other one's delivery date and a late event would be pinned on it.
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e

**Confidence calibration** cuối cùng: entity không `resolved` → ≤ 0.4; còn conflict chưa giải quyết → ≤ 0.7; trừ 0.05 cho mỗi correction; giới hạn [0.05, 0.95], không bao giờ 1.0. Timeline và source precedence được đảm bảo ở tầng specialist/conflict (mục 3–4).

<<<<<<< HEAD
## 7. Reproducibility

- Runtime: Python 3.11.9; package theo range trong `pyproject.toml` (đã kiểm tra với `mcp` 2.2.0, `httpx2` 2.13.1, `jsonschema` 4.26.0).
- Không dùng LLM, không random seed: cùng evidence cho ra cùng output. Giá trị ngẫu nhiên duy nhất là `event_id` của trace.
- Concurrency: case chạy tuần tự; trong một case tối đa 3 specialist song song trên cùng một MCP session.
- Giới hạn: 12 MCP call/case, 2 attempt/call, 60 s/attempt, 8 hop/review chain.
- Lệnh chạy: `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip`.
- Kiểm thử: `pytest -q` (framework test dùng fake gateway, không phát sinh MCP call), `ruff check .`.
=======
### When revisions collide

Two revisions can share a purchase timestamp, and then the windows above cannot separate them. That is detectable rather than silent: a revision bills each `order_item_id` once and holds at most one payment per `payment_sequential`, so more rows than that means the attribution is ambiguous. Duplicate order lines are collapsed first — counting the same line twice would double the order value. The captures are then resolved in this order:

1. The subset that sums to exactly the order value, since the revision under investigation is the one whose payments settle it — `CAPTURE_SETTLES_THE_ORDER_VALUE`.
2. The amount an in-scope refund repays, because a refund is raised against the revision under investigation — `CAPTURE_SCOPED_BY_REFUND_LIFECYCLE`.
3. The captures landing exactly on `order_approved_at`.
4. Failing all of those, when both revisions are byte-identical copies, the set is trimmed to one payment per sequential — `CAPTURE_TRIMMED_TO_SEQUENTIAL_CAPACITY`.
5. Only if nothing narrows it is the conflict reported with `selected_source: null` and `CAPTURE_REVISION_UNRESOLVED`, rather than resolved by guesswork.

`payment_references` lists only the sequentials whose `payment_value` matches a capture kept in scope, so the reported references, `captured_total_brl` and the refund lines all describe the same revision.

## Issue ranking

`classify` reads the scoped signals in a fixed order, and only an explicit signal counts:

1. `order_status == canceled` with a capture → `canceled_order_paid`
2. `order_status == unavailable` with a capture → `unavailable_order_paid`
3. An open `reconciliation_mismatch` event → `payment_mismatch`
4. A repeated in-scope capture whose total exceeds the order value → `duplicate_charge`
5. A `delivered_late` event on the authoritative delivery, by actor → `late_delivery_seller` or `late_delivery_logistics`
6. A failed, then a pending, in-scope refund → `refund_failed`, `refund_pending`
7. Several in-scope captures summing exactly to the order value → `valid_split_payment`
8. A complete timeline and ledger that support nothing → `unsupported_claim`
9. No resolved order or no scoped evidence → `insufficient_evidence`

A capture total that simply differs from the order value is never read as a mismatch: the gateway raises `reconciliation_mismatch` when the ledgers actually disagree, and inferring one from an amount gap would misclassify the freight-only and part-capture cases. The stated claim topic is never an input to this ranking; it is only compared against the result to judge each claim.

## Policy engine

`case_status`, `recommended_action`, `recommended_refund_brl` and `responsible_parties` all come from the `get_policy` rule matching the established issue. Business arbitration belongs to the policy the gateway serves; `contracts/scoring/scoring-policy-v2.json` is the scoring contract, and this workflow reads it for the lifecycle events the trace owes and the rule its confidence values are chosen against.

When no rule matches the established issue, the case stays `needs_investigation` with no refund line rather than receiving an invented amount, and a status of `action_required` with nothing to collect is downgraded for the same reason. Refund lines are reconciled against `recommended_refund_brl` with decimal arithmetic, and `resolution_actions` carries the policy's `recommended_action` de-duplicated. Unknown payment totals stay `null`; they are never reported as zero.

### Accountability

The published table states a party type, and `ACCOUNTABLE_PARTY` is what the verifier holds it against, so the liable party can never drift from the issue the evidence proved:

| Established issue | Answerable party |
| --- | --- |
| `canceled_order_paid` | `platform` |
| `unavailable_order_paid`, `late_delivery_seller` | `seller` |
| `late_delivery_logistics` | `logistics_provider` |
| `payment_mismatch`, `duplicate_charge`, `refund_failed`, `refund_pending` | `payment_provider` |
| `valid_split_payment`, `unsupported_claim` | `customer` |
| `insufficient_evidence` | `unknown` |

A seller delay therefore cannot leave the logistics provider paying, and vice versa. Where the party is a seller, the policy's template seller id is replaced by a seller this order actually has: citing the template would put an entity from outside this case's scope into the result.

## Confidence calibration

Calibration is graded as one minus the squared error between whether the primary issue is right and the confidence submitted, so `confidence` is a belief rather than a flourish. `calibrate` starts from how strong the decisive signal is — an authoritative `order_status`, an explicit `reconciliation_mismatch` or a `delivered_late` event with a named actor carries more than an amount equality, which carries more than the negative finding behind `unsupported_claim` — and then subtracts for every gap in the evidence behind it:

| Evidence condition | Effect |
| --- | --- |
| No policy rule applied | −0.20 |
| Capture revision unresolved | −0.25 |
| Capture revision ambiguous, resolved by refund or instant | −0.08 |
| Capture revision trimmed to sequential capacity | −0.05 |
| Shipment or payment evidence unavailable | −0.10 |
| Incomplete timeline behind a delivery issue | −0.10 |
| Incomplete timeline otherwise | −0.03 |
| Order value unknown behind a split or duplicate finding | −0.15 |
| No confirmed item membership | −0.05 |
| No independent product confirmation | −0.03 |
| A seller is answerable but unconfirmed in the registry | −0.04 |
| More than one revision of this order on record | −0.02 |
| The customer's own account corroborates the finding | +0.03 |

The result is capped at 0.97. It never reaches 1.0, because a second source disagreed somewhere in every case of this set, and `insufficient_evidence` is reported at 0.15 rather than dressed up.

## Failure and efficiency

| Failure | Budget | Result | Trace |
| --- | --- | --- | --- |
| MCP timeout or transport fault | 2 attempts, 30 s each, one 0.2 s pause | Mark that fact unavailable, continue | No `tool_result_consumed` |
| Tool-level refusal | 1 attempt, no retry | The gateway has no such evidence for this scope | No `tool_result_consumed` |
| Envelope fails the contract | No retry | Discarded by `EvidenceGateway.call` | No `tool_result_consumed` |
| Wrong evidence domain | No retry | Discarded, not registered in the ledger | No `tool_result_consumed` |
| Order missing or unverified | Exact lookup only | `entity_resolution.not_found`, `insufficient_evidence` | `verification_completed` |
| Policy rule absent | No retry | `needs_investigation`, no refund line | `policy_decided` |
| Verifier check fails | No retry without new evidence | `solve_case` raises; the runner writes no output | No `case_finalized` |

An exhausted argument set is remembered per case, so no key is retried later in the same investigation. Envelopes are cached only inside one case, references are never shared between cases, and no tool is called with anything but an exact identifier.

## Verification

The verifier runs with no gateway access, so it can only confirm what the specialists brought back. A contradictory case is worth less than an honest `needs_investigation`, so anything self-contradictory raises and the runner writes no output for it.

**Provenance.** Every reference cited in `evidence_refs` or in a `claim_assessments` entry is owned by this case's ledger; a substantive finding cites at least one; each conflict names two distinct sources and selects one it named, or `null`.

**Scope.** No rejected candidate reaches `affected_entities` or the resolved set; a resolved order is present and agrees with `entity_resolution`; an unresolved resolution names no order; an on-time verdict rests on a real delivery timestamp; late sellers are named only under a `seller_delay` verdict and only from this order's sellers.

**Cross-field consistency.** The established issue, the answerable party, both analyses and the ranked causes have to tell one story: the party matches `ACCOUNTABLE_PARTY`, a delivery issue matches the shipment verdict, a payment issue matches the payment verdict, the top ranked cause names the primary issue, ranks do not repeat, actions do not repeat, and `no_action` carries no refund.

**Money.** Refund lines reconcile with the recommended amount, a recommendation has a line to pay it on, `action_required` has something to collect, `refundable_total_brl` follows captured minus refunded, and a refund line names an entity inside this order. Where the policy's amount outruns the remaining refundable ledger, the verifier returns `POLICY_REFUND_EXCEEDS_REFUNDABLE_LEDGER` instead of rewriting the authoritative amount.

**Lifecycle.** Inside the investigation the verifier confirms the events it can see — `task_assigned` before `handoff`, a `policy_decided`, and a `tool_result_consumed` whenever the finding is substantive. `case_received`, `case_finalized` and the verifier's own event belong to the runner, so `day09 validate` checks the whole trace file against the scoring policy's `workflow_required_events`: each case opens with `case_received`, closes with `case_finalized`, carries each required event exactly once where it is a lifecycle bound, and verifies before it finalizes.

## Reproducibility

The implementation is deterministic: fixed sequential specialist order, no model call, no randomness, no wall-clock branch. `day09 validate` re-checks every output and trace event against the contracts, and `day09 package` validates the manifest against `submission-manifest-v2.schema.json`. Secrets stay in `.env`, outside outputs and trace; trace events carry references and observable decision codes only, never payload data or internal reasoning.
>>>>>>> 9a7a84c24ef1cfa8a96be6d80805dc1c9e3b0e8e
