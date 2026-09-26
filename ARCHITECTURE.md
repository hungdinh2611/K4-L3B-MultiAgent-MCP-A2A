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
| Entity/customer (`entity-agent`) | `candidate_order_ids`, `claimed_order_id`, `customer_unique_id_hint` | Xếp hạng và xác minh candidate, loại candidate sai, lấy lịch sử khách hàng | `get_order`, `get_customer_history` | `entity_resolution`, `customer_context`, order row → coordinator |
| Coordinator (`coordinator`) | Case input, handoff của specialist | Giao task, fan-out specialist song song, mở review chain, ghi output | không có | `task_assigned`; nhận `finalize` từ verifier |
| Order/product (`order-agent`) | `order_id` đã resolve | Item, seller, sản phẩm liên quan | `get_order_items`, `get_product_context`, `get_sellers` | `item_ids`, `seller_ids`, giá item → coordinator |
| Shipment (`shipment-agent`) | `order_id` | Timeline giao hàng, seller handoff limit, trễ do seller hay logistics | `get_shipment_summary` | `shipment_analysis`, `shipment_ids` → coordinator |
| Payment/refund (`payment-agent`) | `order_id` | Đối soát capture, lifecycle thanh toán, refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_analysis`, `payment_references` → coordinator |
| Policy (`policy-agent`) | Findings + conflicts, `policy_version` | Áp policy: primary issue, trách nhiệm, số tiền hoàn, actions | `get_policy` | `policy_decided`; draft decision → verifier |
| Conflict resolver (`conflict-agent`) | Findings của mọi specialist | Phát hiện nguồn mâu thuẫn, chọn nguồn authoritative | không có | `data_conflicts` → policy |
| Verifier (`verifier-agent`) | Draft decision + evidence ledger | Kiểm tra invariant, hiệu chỉnh confidence, lắp output | không có | `verification_completed`; `finalize` + output → coordinator |

Áp dụng least privilege: mỗi tool thuộc đúng một actor (có test `test_every_tool_is_owned_by_exactly_one_agent`). Gọi tool ngoài allowlist sẽ ném `ToolPermissionError` trước khi có MCP call.

## 3. Entity resolution và A2A protocol

**Entity resolution** (`entity-agent`):

1. Xếp hạng candidate: `claimed_order_id` trước, sau đó các candidate khác theo thứ tự input. Chỉ ID có định dạng order hợp lệ (32 ký tự hex) mới được tra cứu. Placeholder như `candidate-NNN` bị loại mà không tốn call.
2. `get_order` trên candidate đầu tiên. Nếu tồn tại và khớp scope của case thì `resolved`, các candidate còn lại vào `rejected_candidates`.
3. Nếu không tìm thấy thì thử candidate tiếp theo, tối đa 2 lần tra `get_order`. Không có candidate nào khớp thì `not_found`. Nhiều candidate cùng khớp mà không phân biệt được thì `ambiguous`.
4. Order row không có `customer_unique_id`, nên `get_customer_history` được gọi với `customer_unique_id_hint`. Hint chỉ được chấp nhận khi history chứa đúng order đã resolve với cùng `customer_id`. Nếu không khớp thì `customer_unique_id: null`.
5. **Episode scoping**: cùng một order ID có thể mang nhiều episode (các row có ngày mua khác nhau). `get_order` trả về episode đầu tiên, và đó không nhất thiết là episode của case. Episode của case được chọn trong các episode có `order_purchase_timestamp` ≤ `opened_at` ([timeline.py](src/student_agent/timeline.py)): ưu tiên episode muộn nhất mà order row khớp claim (`canceled` / `unavailable` theo status, trễ giao hoặc giao đúng hạn theo mốc thời gian), nếu không có thì lấy episode muộn nhất. Claim về payment/refund không nhận ra được từ order row nên dùng episode muộn nhất. Event, item row (theo `shipping_limit_date`), capture và refund được gán cho episode theo cửa sổ nửa mở `[purchase của episode, purchase của episode kế tiếp)`. Mọi specialist chỉ phân tích dữ liệu trong cửa sổ này; episode còn lại là nhiễu và được ghi vào `data_conflicts`.
6. Confidence: `resolved` và history xác minh được thì 0.95; `resolved` không có history thì 0.8; `ambiguous` 0.4; `not_found` 0.1. Khi không `resolved`, các specialist theo order không chạy và case chuyển `needs_investigation`.

Thứ tự handoff: entity → order → (shipment ∥ payment). Payment cần `order_value_brl` từ order-agent để phân biệt split payment (tổng capture = giá trị đơn) với duplicate capture (các capture trùng số tiền nhưng tổng lệch giá trị đơn).

**A2A message envelope** (`A2AMessage`): `case_id`, `sender`, `recipient`, `intent`, `payload`, `evidence_refs`. `CaseContext.dispatch` từ chối message nếu:

- `case_id` khác case hiện tại (correlation theo `case_id`, chống dùng chéo case);
- giao sai `recipient`, hoặc agent trả message với `sender` không phải chính nó;
- message trích dẫn `evidence_ref` không có trong ledger của case.

**Handoff**: message từ coordinator emit `task_assigned`; mọi reply emit `handoff` (actor → target, `decision_code` = intent, kèm evidence refs). Review chain là tuyến tính conflict → policy → verifier → coordinator; `route` giới hạn 8 hop nên không thể lặp vô hạn. Trace chỉ ghi sự kiện quan sát được, không ghi nội dung suy luận.

## 4. Evidence và conflict lifecycle

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
| Entity scope | `resolved_order_ids` ⊆ candidate; không giao `rejected_candidates`; `affected_entities.order_ids` = order đã resolve | `ENTITY_OUT_OF_SCOPE`, `REJECTED_OVERLAP`, `AFFECTED_ORDER_MISMATCH` |
| Evidence ownership / claim linkage | Mọi ref trong output có trong ledger của case **và** đã có `tool_result_consumed`; ref của claim ⊆ `evidence_refs` | `UNTRACED_EVIDENCE` |
| Payment/refund totals | refundable = captured − refunded ≥ 0; refund ≤ refundable; refund = tổng `refund_lines` | `REFUNDABLE_RECOMPUTED`, `REFUND_CAPPED`, `REFUND_LINES_TOTAL` |
| Status/action | Action không trùng; `no_action` thì refund 0, không có refund line, action `document_no_action` | `DUPLICATE_ACTIONS`, `NO_ACTION_WITH_REFUND` |
| Responsibility | `late_seller_ids` ⊆ `seller_ids` và chỉ có khi `seller_delay`; party seller ∈ `seller_ids`; party khác không mang ID ngoài case | `LATE_SELLER_SCOPE`, `SELLER_PARTY_SCOPE`, `FOREIGN_PARTY_ID` |
| Schema | Output đúng `l3b-output-v2.schema.json` | exception (lỗi lập trình, không bịa output) |

**Confidence calibration** cuối cùng: entity không `resolved` → ≤ 0.4; còn conflict chưa giải quyết → ≤ 0.7; trừ 0.05 cho mỗi correction; giới hạn [0.05, 0.95], không bao giờ 1.0. Timeline và source precedence được đảm bảo ở tầng specialist/conflict (mục 3–4).

## 7. Reproducibility

The runner clears prior outputs after MCP discovery, then writes directly to
`traces/trace.jsonl` as events occur and to `outputs/<case_id>.json` after each case.
Packaging checks that every output reference appeared in a `tool_result_consumed` event for
the same case and that each case has the required lifecycle events in order. Item rows are
restricted to the selected episode; a missing shipment summary falls back to the scoped order
and item dates.

`case_finalized.attributes` records `mcp_calls_attempted` and `mcp_tools_failed` for each
case. The runner also prints their totals. These are client-side diagnostics and include failed
attempts, while `tool_result_consumed` records only successful evidence. They do not change the
MCP server's audit or the scoring policy.

The active split-agent regression tests are `test_cli_run.py`, `test_a2a_framework.py`,
`test_specialists.py`, `test_review_chain.py`, and `test_starter.py`. `test_workflow.py` still imports the older
monolithic `agents.py` API; its collection failure is unrelated to the split-agent runner.
`test_release_safety.py` assumes the competition payload has not been downloaded, which is
false in a local workspace after a scored run.

- Runtime: Python 3.11.9; package theo range trong `pyproject.toml` (đã kiểm tra với `mcp` 2.2.0, `httpx2` 2.13.1, `jsonschema` 4.26.0).
- Không dùng LLM, không random seed: cùng evidence cho ra cùng output. Giá trị ngẫu nhiên duy nhất là `event_id` của trace.
- Concurrency: case chạy tuần tự; trong một case tối đa 3 specialist song song trên cùng một MCP session.
- Giới hạn: 12 MCP call/case, 2 attempt/call, 60 s/attempt, 8 hop/review chain.
- Lệnh chạy: `day09 validate-inputs` → `day09 run` → `day09 validate` → `day09 package --output dist/submission.zip`.
- Kiểm thử: `pytest -q` (framework test dùng fake gateway, không phát sinh MCP call), `ruff check .`.
