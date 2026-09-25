# Hướng dẫn Lab: Multi-Agent MCP A2A

| Pha | Nội dung | Thời gian |
|-----|----------|-----------|
| [1](#pha-1-đăng-ký-team-môi-trường--mcp-gateway-ping) | Đăng ký team, môi trường & MCP Gateway ping | 0 – 30 phút |
| [2](#pha-2-thiết-kế-multi-agent-a2a--contract-schemas) | Thiết kế Multi-Agent A2A & Contract Schemas | 30 – 65 phút |
| [3](#pha-3-triển-khai-specialist-agents--mcp-gateway) | Triển khai Specialist Agents & MCP Gateway | 65 – 110 phút |
| [4](#pha-4-policy-engine-verifier--calibration) | Policy Engine, Verifier & Calibration | 110 – 150 phút |
| [5](#pha-5-tải-inputs-batch-run-100-cases--xác-thực) | Tải inputs, batch run 100 cases & xác thực | 150 – 210 phút |
| [6](#pha-6-đóng-gói-zip-nộp-bài--github) | Đóng gói ZIP, nộp bài & GitHub | 210 – 240 phút |

---

## PHA 1: Đăng ký team, môi trường & MCP Gateway ping

> ⏱ **0 – 30 phút**

**Mục tiêu:** Ghi nhận quá trình làm bài của từng thành viên và nhóm trên hệ thống thi, dựng môi trường Python với CLI `day09`, và kết nối được tới MCP Evidence Gateway.

### 1.1. Đăng ký team và nhận API Key

1. Tất cả thành viên truy cập: <https://n7-competition.pages.dev/register>
2. Điền đầy đủ thông tin:
   - **Tên Team:** theo cú pháp `K4-TeamXX-<TenNhom>`
   - **Mã học viên cá nhân & danh sách thành viên:** *mọi thành viên* đều phải đăng ký và khai báo các thành viên còn lại trong team.
   - **Mã đăng ký (Registration Code):** do Lab Coach công bố tại lớp.
3. Nhận **Team API Key** dạng `sk-team-...`

> [!CAUTION]
> Key chỉ hiển thị **DUY NHẤT 01 LẦN**, hãy sao chép ngay.
> - Không gửi key qua kênh chat công khai.
> - Không commit key vào Git repo.
>
> Lộ key sẽ ảnh hưởng đến kết quả cá nhân của bạn.

### 1.2. Fork và clone repo chính thức

1. Đại diện nhóm mở repo của lớp mình:
   - **Lớp B:** <https://github.com/VinUni-AI20k/K4-L3B-MultiAgent-MCP-A2A>
2. Nhấn **Fork**, giữ nguyên tên repo gốc.
3. Clone về máy:

   ```bash
   git clone <url_repo_fork_cua_nhom>
   cd K4-L3B-MultiAgent-MCP-A2A   # hoặc K4-L3A-... tùy lớp
   ```

### 1.3. Khởi tạo môi trường & cài gói `day09`

Yêu cầu **Python 3.11 trở lên**.

```bash
# 1. Tạo môi trường ảo
python -m venv .venv

# 2. Kích hoạt môi trường ảo
.venv\Scripts\Activate.ps1        # Windows PowerShell
source .venv/bin/activate         # Linux / macOS

# 3. Cài package ở chế độ editable kèm dev dependencies
python -m pip install -e ".[dev]"
```

### 1.4. Cấu hình biến môi trường (`.env`)

Sao chép `.env.example` thành `.env`:

```bash
cp .env.example .env               # Linux / macOS
Copy-Item .env.example .env        # Windows PowerShell
```

Nội dung `.env`:

```env
COMPETITION_API_URL=url_competition
COMPETITION_TEAM_API_KEY=sk-team-your_key_here
MCP_ENDPOINT=http://domain/mcp
```

> [!TIP]
> Nếu nhóm dùng LLM bên ngoài (OpenAI, Gemini, Claude, ...), có thể khai báo thêm biến môi trường tại đây. Repo là của nhóm, cứ tùy chỉnh sao cho giải quyết bài nhanh và chính xác nhất.

### 1.5. Kiểm tra kết nối MCP Evidence Gateway

Chạy bộ test starter và liệt kê MCP tools:

```bash
pytest -q
day09 --help
day09 mcp-tools
```

---

## PHA 2: Thiết kế Multi-Agent A2A & Contract Schemas

> ⏱ **30 – 65 phút**

**Mục tiêu:** Thiết lập kiến trúc phối hợp đa tác tử (A2A), phân định trách nhiệm từng agent và cố định schema chuẩn trong `contracts/schemas/`.

### Kiến trúc kỳ vọng

```text
                          ┌──────────────────────────┐
                          │   Coordinator / Router   │
                          └─────────────┬────────────┘
                                        │ (Handoff)
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                   [END OUTPUT]
```

### 2.1. Tuân thủ Public Contracts (`contracts/schemas/`)

| File | Vai trò |
|------|---------|
| `l3a-output-v2.schema.json` (hoặc `l3b`) | Schema bắt buộc cho output từng case |
| `trace-event-v1.schema.json` | Schema cho trace log |
| `submission-manifest-v2.schema.json` | Schema cho manifest khi đóng gói nộp bài |
| `mcp-evidence-response-v1.schema.json` | Cấu trúc envelope trả về từ MCP Gateway |

> [!IMPORTANT]
> **Không thêm bất kỳ field nào ngoài schema.** Khi có sai lệch, JSON Schema luôn là chuẩn ưu tiên cao nhất. Ràng buộc chặt, nhưng công bằng.

### 2.2. Tự do chọn framework

Điểm triển khai chính nằm ở `src/student_agent/workflow.py`:

```python
async def solve_case(case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> dict[str, Any]:
    # Triển khai coordinator và specialist agents tại đây
    ...
```

Bài không chấm theo tên framework: có thể dùng LangGraph, Semantic Kernel, CrewAI hoặc state machine thuần Python async. Hệ thống chỉ đánh giá:

- kết quả nghiệp vụ,
- tính hợp lệ của bằng chứng MCP,
- trace log.

### 2.3. Hoàn thiện `ARCHITECTURE.md`

Mô tả:

- luồng handoff giữa các agent,
- tool permissions của từng agent,
- cơ chế retry khi MCP gặp sự cố.

---

## PHA 3: Triển khai Specialist Agents & MCP Gateway

> ⏱ **65 – 110 phút**

**Mục tiêu:** Mỗi agent chuyên trách truy vấn bằng chứng có thẩm quyền qua MCP Evidence Gateway, đúng scope của từng case, và ghi trace audit.

### Nguyên tắc của MCP Gateway

> [!WARNING]
> Đọc kỹ bảng dưới để tránh mất điểm.

| # | Nguyên tắc | Nếu vi phạm |
|---|------------|-------------|
| 1 | Truyền đúng `case_id` cho mọi MCP call | Bị từ chối truy cập (`403 Forbidden`) |
| 2 | **KHÔNG** tự sinh hoặc sửa đổi `evidence_ref` | ❌ **Hard Gate: 0 điểm toàn bài** |
| 3 | Chỉ trích dẫn evidence thực sự hỗ trợ kết luận | Trừ điểm phần *Evidence Relevance* |
| 4 | Ghi event `tool_result_consumed` vào trace | Không được công nhận tính xác thực |
| 5 | Server lưu audit độc lập (hash, latency, status) | Trace client bị giả mạo sẽ bị phát hiện |

### 3.1. Gọi tool qua Gateway

```python
# Lấy dữ liệu đơn hàng có thẩm quyền từ MCP
evidence = await gateway.call(
    "get_order",
    case_id=case["case_id"],
    order_id=order_id,
)
evidence_ref = evidence["evidence_ref"]
order_data = evidence["data"]
```

### 3.2. Ghi trace event hợp lệ

```python
# Ghi nhận sự kiện tiêu thụ bằng chứng vào trace audit
trace.emit(
    case_id=case["case_id"],
    event_type="tool_result_consumed",
    actor="order-agent",
    tool_name="get_order",
    evidence_refs=[evidence_ref],
)
```

---

## PHA 4: Policy Engine, Verifier & Calibration

> ⏱ **110 – 150 phút**

**Mục tiêu:** Áp dụng chính sách xử lý tranh chấp, kiểm tra chéo mâu thuẫn dữ liệu và hiệu chuẩn độ tin cậy.

### 4.1. Policy Agent

Ra quyết định dựa trên `contracts/scoring/scoring-policy-v2.json`:

| Thành phần | Mô tả |
|------------|-------|
| **Primary Issue** | Lỗi cốt lõi: `canceled_order_paid`, `late_delivery_seller`, `late_delivery_logistics`, `payment_mismatch`, ... |
| **Responsible Party** | Bên chịu trách nhiệm: `seller`, `platform`, `logistics_provider`, `payment_provider`, `customer` |
| **Financial Resolution** | Số tiền hoàn chính xác: `recommended_refund_brl`, `refund_lines` |
| **Resolution Actions** | Các hành động cụ thể cần thực hiện |

### 4.2. Verifier Agent & Confidence Calibration

- **Cross-field consistency:** `primary_issue`, `responsible_parties` và `financial_resolution` phải logic và nhất quán với nhau.
  *Ví dụ:* lỗi do người bán thì đơn vị vận chuyển không thể chịu trách nhiệm hoàn tiền.
- **Confidence calibration:** `confidence` nằm trong `[0.0, 1.0]`, dựa trên chất lượng và độ đầy đủ của evidence. Không đặt `1.0` khi bằng chứng có mâu thuẫn.
- **Lifecycle events:** `traces/trace.jsonl` phải có đủ các event bắt buộc theo thứ tự:

  ```text
  case_received → task_assigned → tool_result_consumed → handoff
               → policy_decided → verification_completed → case_finalized
  ```

---

## PHA 5: Tải inputs, batch run 100 cases & xác thực

> ⏱ **150 – 210 phút**

**Mục tiêu:** Tải bộ đề từ GitHub Release, chạy batch toàn bộ cases và kiểm tra tính toàn vẹn của outputs và trace log.

### 5.1. Tải và giải nén test bundle

1. Tải file ZIP input tương ứng (`l3a-inputs-*.zip` hoặc `l3b-inputs-*.zip`) từ GitHub Release và giải nén vào repo (vị trí tùy nhóm, không bắt buộc):

   ```bash
   unzip l3b-inputs-*.zip -d .
   ```

2. Cấu trúc sau khi giải nén:

   ```text
   case-set.json
   inputs/
   ├── L3A_CASE_001.json
   ├── ...
   └── L3A_CASE_100.json
   ```

3. Xác thực tập input:

   ```bash
   day09 validate-inputs
   ```

> ✅ **Pass signal 5.1:** `OK: l3a / v2.1 / 100 cases`

### 5.2. Chạy batch toàn bộ cases

```bash
day09 run
```

Lệnh này lặp qua từng case trong `case-set.json`, gọi `solve_case` trong `workflow.py` và tạo ra:

- `outputs/<case_id>.json`: đủ 100 cases
- `traces/trace.jsonl`: toàn bộ timeline sự kiện

### 5.3. Kiểm tra kết quả trước khi đóng gói

```bash
day09 validate
```

> ✅ **Pass signal 5.2:** `OK: 100 outputs / <N> trace events`

---

## PHA 6: Đóng gói ZIP, nộp bài & GitHub

> ⏱ **210 – 240 phút**

### 6.1. Cấu trúc ZIP submission

`submission.zip` **chỉ chứa 3 thành phần nằm ngay ở gốc ZIP**:

```text
submission.zip
├── manifest.json
├── trace.jsonl
└── outputs/
    ├── L3A_CASE_001.json
    ├── L3A_CASE_002.json
    └── ... (đúng 100 files)
```

> [!CAUTION]
> **Không có thư mục bọc ngoài.** Nếu có, hệ thống giải nén ra không đọc được file và chấm **0 điểm**.

**Chi tiết 3 thành phần:**

1. **`manifest.json`**: metadata theo chuẩn `submission-manifest-v2.schema.json`:

   ```json
   {
     "schema_version": "day09-submission-manifest-v2",
     "competition_id": "day09-multiagent-mcp-a2a",
     "variant_id": "l3a",
     "case_set_version": "v2.1",
     "output_schema_version": "day09-l3a-output-v2",
     "trace_schema_version": "day09-trace-event-v1",
     "generated_at": "2026-09-25T08:00:00Z",
     "client": {
       "name": "day09-student-starter",
       "version": "0.1.0"
     }
   }
   ```

2. **`trace.jsonl`**: toàn bộ audit event của các agent, đặt ngay ở gốc ZIP.
3. **`outputs/`**: đúng 100 file JSON kết quả, mỗi file một case.

> [!WARNING]
> **Không** đưa vào ZIP: mã nguồn `src/`, file `.env`, Team API Key, raw inputs hay debug logs.

### 6.2. Đóng gói bằng CLI

Để tránh sai sót thủ công, dùng lệnh đóng gói có sẵn:

```bash
day09 package --output dist/submission.zip
```

> ✅ **Pass signal 6:** `OK: .../dist/submission.zip`

Lệnh này tự động:

- validate 100 outputs,
- validate trace events,
- tạo manifest V2,
- kiểm tra dung lượng,
- quét regex phát hiện lộ API Key trước khi xuất file ZIP.

### 6.3. Nộp bài lên Competition Workspace

1. Vào trang nộp bài và upload file ZIP.
2. Hệ thống đưa bài vào hàng đợi và chấm tự động, chờ kết quả.
3. Nếu nhóm không nằm trong top 10, dùng bộ lọc để tìm nhóm mình trên bảng xếp hạng.
