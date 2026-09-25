# Architecture

## 1. Agent Roles

### Coordinator
- Phân tích `case` đầu vào (case_id, customer context, claims, issue description).
- Route task có điều kiện đến các specialist agents dựa trên ngữ cảnh (`need_order`, `need_shipment`, `need_payment`, `need_policy`).
- Quản lý lifecycle và phát ra các trace events observable A2A (`task_assigned`, `handoff`).
- Không gọi trực tiếp các tool nghiệp vụ ngoài tool discovery.

### Order/Item Agent
- Chịu trách nhiệm về entity resolution (đối chiếu candidate order IDs, customer history).
- Thu thập evidence về order, order items, sellers và product context.
- Trả về finding nội bộ: `status`, `resolved_order_ids`, `rejected_candidates`, items, sellers, evidence_refs, confidence.

### Shipment Agent
- Thu thập evidence về shipment summary, carrier tracking, và delivery milestones.
- Đối chiếu ngày giao hàng dự kiến (`order_estimated_delivery_date`) và ngày giao hàng cho carrier với hạn chót của seller (`shipping_limit_date`).
- Xác định verdict vận chuyển (`on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`, `insufficient_evidence`).

### Payment Agent
- Thu thập evidence về order payments, payment timeline và refund timeline.
- Tính toán tổng tiền đã capture, đã refund, và số tiền còn có thể refund (`captured_total_brl`, `refunded_total_brl`, `refundable_total_brl`).
- Kiểm tra tính hợp lệ của thanh toán, phát hiện duplicate charge, split payment hợp lệ, hoặc refund thất bại.

### Policy Agent
- Đối chiếu evidence tổng hợp từ các specialist với business policy (`get_policy`).
- Đánh giá từng claim của khách hàng (`claim_assessments`).
- Xác định primary issue, secondary issues, root cause analysis (`ranked_causes`, `responsible_parties`), data conflicts và giải pháp tài chính (`financial_resolution`).
- Emit trace event `policy_decided`.

### Verifier Agent
- Chạy cuối cùng trong quy trình, kiểm tra toàn bộ invariants trước khi finalize.
- Kiểm tra `case_id`, required fields theo đúng `l3b-output-v2.schema.json`.
- Xác minh `evidence_refs`: chỉ dùng evidence thực tế được thu thập trong case, không fabrications, không dùng chéo case.
- Kiểm tra tính nhất quán (consistency, calibration bounds, financial balance, enum values).
- Không gọi MCP mới theo mặc định để đảm bảo tính tất định và tiết kiệm chi phí gọi tool.
- Emit trace event `verification_completed` và trả về JSON output hợp lệ.


## 2. Handoff Flow

```text
Case Input
    │
    ▼
Coordinator ──(task_assigned / handoff)──► Order/Item Agent (Entity Resolution & Items)
    │                                              │
    ├──(task_assigned / handoff)──► Shipment Agent (Shipment & Delivery Timeline)
    │                                              │
    ├──(task_assigned / handoff)──► Payment Agent  (Payments & Refund Lifecycle)
    │                                              │
    ▼                                              ▼
Coordinator ──(task_assigned / handoff)──► Policy Agent    (Policy Reconciliation & Root Cause)
    │                                              │
    ▼                                              ▼
Coordinator ──(task_assigned / handoff)──► Verifier Agent  (Invariants, Schema & Provenance)
                                                   │
                                                   ▼
                                              Final Output (l3b-output-v2)
```


## 3. MCP Tool Permissions Matrix

Áp dụng nguyên tắc đặc quyền tối thiểu (least privilege) cho từng agent:

| Agent | Allowed Domain | Allowed MCP Tools |
|---|---|---|
| **Coordinator** | routing / discovery | `list_tools` |
| **Order/Item Agent** | customer / order / item / product | `get_customer_history`, `get_order`, `get_order_items`, `get_sellers`, `get_product_context` |
| **Shipment Agent** | shipment / logistics | `get_shipment_summary` |
| **Payment Agent** | payment / refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` |
| **Policy Agent** | policy / rules | `get_policy` |
| **Verifier Agent** | invariants check | No MCP tools by default (offline verification) |


## 4. MCP Evidence Rules & Lifecycle

- **Case Scope Isolation**: Mọi request đến MCP tool đều bắt buộc truyền đúng `case_id`. Tuyệt đối không tái sử dụng evidence giữa các `case_id` khác nhau.
- **Evidence Provenance & Integrity**:
  - `evidence_ref` được sao chép nguyên vẹn 100% từ thuộc tính `evidence_ref` của Gateway.
  - Tuyệt đối không tự sinh, không sửa đổi hay băm lại `evidence_ref`.
- **Observable Trace Audit**:
  - Khi một Specialist Agent thực sự tiêu thụ dữ liệu từ MCP để đưa ra nhận định, hệ thống phát ngay sự kiện `tool_result_consumed` với `case_id`, `actor`, `tool_name` và `evidence_refs`.
  - Không emit `tool_result_consumed` cho các tool call thất bại hoặc không được dùng.
- **Per-Case In-Memory Cache**:
  - Kết quả của mỗi tool call được cache trong phạm vi từng case qua key `(tool_name, sorted_kwargs)`.
  - Tránh gọi lại cùng một tool với cùng tham số nhiều lần trong một case, tối ưu điểm hiệu quả MCP (`efficiency`).
  - Cache được giải phóng ngay khi chuyển sang case tiếp theo.
- **Bounded Retry Strategy**:
  - Giới hạn tối đa 2 lần gọi (1 retry) với backoff ngắn (`0.15s * attempt`) khi gặp sự cố mạng hoặc gateway lỗi tạm thời.
  - Khi tool thất bại hoàn toàn sau 2 lần thử, agent ghi nhận `insufficient_evidence` và tiếp tục quy trình an toàn, tuyệt đối không tạo dữ liệu giả lập.
- **Selective Routing Policy**:
  - Coordinator dựa vào nội dung khiếu nại của khách hàng để chỉ đánh thức các Specialist Agent thực sự liên quan (ví dụ: case khiếu nại giao trễ sẽ chỉ gọi Order + Shipment + Policy, bỏ qua Payment).
