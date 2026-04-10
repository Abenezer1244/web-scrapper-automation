# Tracerfy API Documentation

Complete API reference for Tracerfy's skip tracing and DNC scrubbing platform.

**Base URL:** `https://tracerfy.com`

**Authentication:** All endpoints require a Bearer token in the `Authorization` header.

---

## Table of Contents

- [Fetch all Queue](#queues)
- [Fetch Single Queue](#queue)
- [Analytics](#analytics)
- [Batch Trace](#trace)
- [Instant Trace Lookup](#instant-trace)
- [Trace Webhooks](#trace-webhooks)
- [Start DNC Scrub](#dnc-scrub)
- [DNC Scrub from Trace](#dnc-scrub-from-queue)
- [Fetch DNC Queue](#dnc-queue)
- [DNC Webhooks](#dnc-webhooks)

---

## GET Fetch all Queue

`GET /v1/api/queues/`

Returns all queues for the authenticated user. Each queue represents a trace job created via API or the app. While a queue is pending, the serializer hides rows_uploaded and credits_deducted for API queues. When complete, download_url is populated with a public CSV link.

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |

### Example Request

```bash
curl -X GET 'https://tracerfy.com/v1/api/queues/' -H 'Authorization: Bearer <YOUR_TOKEN>'
```

### Example Response (200)

```json
[
  {
    "id": 123,
    "created_at": "2025-01-01T12:00:00Z",
    "pending": false,
    "download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/9a584124-77c2-4612-b8e9-f9efe6fbdc3d.csv",
    "rows_uploaded": 2500,
    "credits_deducted": 2500,
    "queue_type": "api",
    "trace_type": "normal",
    "credits_per_lead": 1
  }
]
```

---

## GET Fetch Single Queue

`GET /v1/api/queue/:id`

Returns the property records associated with a queue's posted addresses. Object-level permission enforced: only the queue owner can access. Null contact fields are normalized to empty strings in the response. 

**Response varies based on trace_type:**
• **Normal Trace** (trace_type='normal'): Returns basic property contact data (phones and emails)
• **Advanced Trace** (trace_type='advanced'): Finds the property owner and returns their contact data (name, phones, emails and mailing address)
• **Custom Trace** (trace_type='custom'): Returns basic property contact data (phones, emails, mailing address)

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |

### Parameters

| Name | In | Type | Required | Description |
|------|-----|------|----------|-------------|
| `:id` | path | `integer` | Yes | Queue ID |

### Example Request

```bash
curl -X GET 'https://tracerfy.com/v1/api/queue/123' -H 'Authorization: Bearer <YOUR_TOKEN>'
```

### Example Response (200)

```json
// Normal Trace Response (trace_type='normal')
[
  {
    "address": "123 Main St",
    "city": "Austin",
    "state": "TX",
    "mail_address": "PO Box 111",
    "mail_city": "Austin",
    "mail_state": "TX",
    "first_name": "Jane",
    "last_name": "Doe",
    "primary_phone": "5125550100",
    "primary_phone_type": "Mobile",
    "email_1": "jane@example.com",
    "email_2": "",
    "email_3": "",
    "email_4": "",
    "email_5": "",
    "mobile_1": "5125550100",
    "mobile_2": "",
    "mobile_3": "",
    "mobile_4": "",
    "mobile_5": "",
    "landline_1": "",
    "landline_2": "",
    "landline_3": ""
  }
]
```

---

## GET Analytics

`GET /v1/api/analytics/`

Aggregated summary for your account: total_queues, properties_traced (sum of posted addresses per queue), queues_pending, queues_completed, and current credit balance.

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |

### Example Request

```bash
curl -X GET 'https://tracerfy.com/v1/api/analytics/' -H 'Authorization: Bearer <YOUR_TOKEN>'
```

### Example Response (200)

```json
{
  "total_queues": 12,
  "properties_traced": 18350,
  "queues_pending": 2,
  "queues_completed": 10,
  "balance": 940
}
```

---

## POST Batch Trace

`POST /v1/api/trace/`

Asynchronous batch endpoint for processing multiple addresses at once via CSV or JSON. Specify trace_type='normal' (1 credit/lead) or 'advanced' (2 credits/lead). Cleans and de-duplicates rows, then enqueues processing in the background. If credits are insufficient the request is rejected. Returns a queue_id immediately along with `estimated_wait_seconds` (estimated processing time in seconds); results are delivered via download_url when complete. For single-address real-time lookups, use the Instant Trace Lookup endpoint instead.

⚠️ API Usage Policy: Do not abuse API POST calls. Accounts found to be abusing the API will be put on hold. Maximum rate limit is 10 POST trace requests per 5-minute window. Please use the API responsibly and in accordance with our Terms of Service - API Rate Limits &amp; Abuse Policy.

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |
| `Content-Type` | `multipart/form-data or application/json` |

### Parameters

| Name | In | Type | Required | Description |
|------|-----|------|----------|-------------|
| `address_column` | body | `string` | Yes | Column for property address |
| `city_column` | body | `string` | Yes | Column for property city |
| `state_column` | body | `string` | Yes | Column for property state |
| `zip_column` | body | `string` | No | Property ZIP (**optional — for advanced traces**, we return this from our data if not provided) |
| `first_name_column` | body | `string` | Yes | Owner first name (**optional for advanced traces** — we identify the owner for you) |
| `last_name_column` | body | `string` | Yes | Owner last name (**optional for advanced traces** — we identify the owner for you) |
| `mail_address_column` | body | `string` | Yes | Mailing address (**optional for advanced traces** — we return this from our data) |
| `mail_city_column` | body | `string` | Yes | Mailing city (**optional for advanced traces** — we return this from our data) |
| `mail_state_column` | body | `string` | Yes | Mailing state (**optional for advanced traces** — we return this from our data) |
| `mailing_zip_column` | body | `string` | No | Mailing ZIP (**optional — for advanced traces**, we return this from our data if not provided) |
| `trace_type` | body | `string` | No | Trace type: 'normal' (1 credit/lead) or 'advanced' (2 credits/lead). Defaults to 'normal'. For advanced traces, only address_column, city_column, and state_column are required — all other fields are optional as we identify the owner and return their full contact and mailing info. |
| `csv_file` | form-data | `file` | Yes | CSV file of records |
| `json_data` | body | `string` | No | Raw JSON array of records (alternative to csv_file) |

### Example Request

```bash
curl -X POST 'https://tracerfy.com/v1/api/trace/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -F 'csv_file=@/path/to/records.csv' \
  -F 'address_column=address' \
  -F 'city_column=city' \
  -F 'state_column=state' \
  -F 'zip_column=zip' \
  -F 'first_name_column=first_name' \
  -F 'last_name_column=last_name' \
  -F 'mail_address_column=mail_address' \
  -F 'mail_city_column=mail_city' \
  -F 'mail_state_column=mail_state' \
  -F 'mailing_zip_column=mailing_zip' \
  -F 'trace_type=normal'
```

### Example Response (200)

```json
{
  "message": "Queue created",
  "queue_id": 456,
  "status": "pending",
  "created_at": "2025-01-02T10:15:00Z",
  "rows_uploaded": 100,
  "trace_type": "normal",
  "credits_per_lead": 1,
  "estimated_wait_seconds": 30
}
```

---

## POST Instant Trace Lookup

`POST /v1/api/trace/lookup/`

Synchronous single-address skip trace. Returns responses immediately as JSON — no queue and no CSV. Ideal for one-off lookups or integrating skip trace data into your own UI at scale.

**5 credits per hit, 0 credits on miss.** Rate limited to 500 RPM per user.

**Two lookup modes:**
• **find_owner: true** (default) — send only address/city/state, returns the property owner(s) and their contact info
• **find_owner: false** — include first_name + last_name to search for a specific person at the address

**Response includes per person:** name, age, DOB, deceased flag, property owner flag, litigator flag, mailing address, all phones (with DNC status, carrier, type, rank), and all emails.

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |
| `Content-Type` | `application/json` |

### Parameters

| Name | In | Type | Required | Description |
|------|-----|------|----------|-------------|
| `address` | body | `string` | Yes | Property street address |
| `city` | body | `string` | Yes | Property city |
| `state` | body | `string` | Yes | Property state (2-letter abbreviation) |
| `zip` | body | `string` | No | Property ZIP code. Optional but **strongly recommended** — without it, results may match a different property at a similar address in the same city. |
| `find_owner` | body | `boolean` | No | `true` (default) — find property owner, no name needed. `false` — find a specific person at the address, requires first_name + last_name. |
| `first_name` | body | `string` | No | Person's first name. **Required when find_owner is false.** |
| `last_name` | body | `string` | No | Person's last name. **Required when find_owner is false.** |

### Example Request

```bash
# Owner lookup (find property owner)
curl -X POST 'https://tracerfy.com/v1/api/trace/lookup/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"address": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701", "find_owner": true}'

# Person lookup (find specific person at address)
curl -X POST 'https://tracerfy.com/v1/api/trace/lookup/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"address": "123 Main St", "city": "Austin", "state": "TX", "zip": "78701", "find_owner": false, "first_name": "Jane", "last_name": "Doe"}'
```

### Example Response (200)

```json
// Owner lookup hit (find_owner: true) — 5 credits deducted
{
  "address": "123 Main St",
  "city": "Austin",
  "state": "TX",
  "zip": "78701",
  "find_owner": true,
  "hit": true,
  "persons_count": 1,
  "credits_deducted": 5,
  "persons": [
    {
      "first_name": "Jane",
      "last_name": "Doe",
      "full_name": "Jane Doe",
      "dob": "1985-03-22",
      "age": "41",
      "deceased": false,
      "property_owner": true,
      "litigator": false,
      "mailing_address": {
        "street": "PO Box 111",
        "city": "Austin",
        "state": "TX",
        "zip": "78702"
      },
      "phones": [
        {
          "number": "5125550100",
          "type": "Mobile",
          "dnc": false,
          "carrier": "T-MOBILE USA INC.",
          "rank": 1
        },
        {
          "number": "5125550200",
          "type": "Landline",
          "dnc": true,
          "carrier": "AT&T TEXAS",
          "rank": 2
        }
      ],
      "emails": [
        {
          "email": "jane.doe@example.com",
          "rank": 1
        }
      ]
    }
  ]
}

// Person lookup hit (find_owner: false) — 5 credits deducted
{
  "address": "123 Main St",
  "city": "Austin",
  "state": "TX",
  "zip": "78701",
  "find_owner": false,
  "hit": true,
  "persons_count": 1,
  "credits_deducted": 5,
  "persons": [
    {
      "first_name": "John",
      "last_name": "Smith",
      "full_name": "John Smith",
      "dob": "1978-11-03",
      "age": "47",
      "deceased": false,
      "property_owner": false,
      "litigator": false,
      "mailing_address": {
        "street": "456 Oak Ave",
        "city": "Dallas",
        "state": "TX",
        "zip": "75201"
      },
      "phones": [
        {
          "number": "2145550300",
          "type": "Mobile",
          "dnc": false,
          "carrier": "VERIZON WIRELESS",
          "rank": 1
        }
      ],
      "emails": [
        {
          "email": "john.smith@example.com",
          "rank": 1
        }
      ]
    }
  ]
}

// Miss — no results found, 0 credits deducted
{
  "address": "999 Nowhere Blvd",
  "city": "Austin",
  "state": "TX",
  "zip": "78701",
  "find_owner": true,
  "hit": false,
  "persons_count": 0,
  "credits_deducted": 0,
  "persons": []
}
```

---

## POST Trace Webhooks

`POST Account.webhook_url`

When a batch skip trace queue completes, Tracerfy POSTs the result to the webhook URL configured in your account profile. This is per-user and dynamic; no registration endpoint is required.

### Headers

| Name | Value |
|------|-------|
| `Content-Type` | `application/json` |

### Example Request

```bash
Tracerfy sends this JSON to your Account.webhook_url when a trace completes.
```

### Example Response (200)

```json
{
  "id": 365,
  "created_at": "2025-07-13T18:55:02.962332Z",
  "pending": false,
  "download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/9a584124-77c2-4612-b8e9-f9efe6fbdc3d.csv",
  "rows_uploaded": 12,
  "credits_deducted": 12,
  "queue_type": "api",
  "trace_type": "normal",
  "credits_per_lead": 1
}
```

---

## POST Start DNC Scrub

`POST /v1/api/dnc/scrub/`

Submit a phone list for DNC (Do Not Call) scrubbing. Upload a CSV with one or more phone columns, or pass a JSON array of phone numbers directly. Each phone is checked against Federal DNC, State DNC, DMA, and TCPA Litigator databases. 1 credit per phone checked.

**Input options (pick one):**
• **CSV with single column**: csv_file + phone_column (string)
• **CSV with multiple columns**: csv_file + phone_columns (array) — phones are merged &amp; deduplicated
• **JSON phone list**: phones array via application/json

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |
| `Content-Type` | `multipart/form-data or application/json` |

### Parameters

| Name | In | Type | Required | Description |
|------|-----|------|----------|-------------|
| `csv_file` | form-data | `file` | Yes | CSV file containing phone numbers. Required for Options 1 & 2. Do not send with phones. |
| `phone_column` | body | `string` | Yes | Single column name containing phone numbers (Option 1). Internally normalized to phone_columns. Mutually exclusive with phone_columns. |
| `phone_columns` | body | `array[string]` | Yes | List of column names containing phone numbers (Option 2). Phones are merged & deduplicated. When multiple columns are used, labels are prefixed with the column name, e.g. '(Phone_1) John Doe'. Mutually exclusive with phone_column. |
| `label_column` | body | `string` | No | Single column to label each phone (e.g., name). Internally normalized to label_columns. Mutually exclusive with label_columns. |
| `label_columns` | body | `array[string]` | No | List of columns to combine as a label for each phone (e.g., ["address", "city", "state"]). Values are joined with commas. When using multiple phone_columns, labels are also prefixed with the column name. |
| `phones` | body | `array[string]` | Yes | Direct list of phone numbers via JSON body (Option 3). Do not send with csv_file. |

### Example Request

```bash
# Option 1: CSV with a single phone column
curl -X POST 'https://tracerfy.com/v1/api/dnc/scrub/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -F 'csv_file=@/path/to/phones.csv' \
  -F 'phone_column=Phone' \
  -F 'label_column=Name'

# Option 1b: CSV with multiple label columns
curl -X POST 'https://tracerfy.com/v1/api/dnc/scrub/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -F 'csv_file=@/path/to/phones.csv' \
  -F 'phone_column=Phone' \
  -F 'label_columns=["Address", "City", "State"]'

# Option 2: CSV with multiple phone columns
curl -X POST 'https://tracerfy.com/v1/api/dnc/scrub/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -F 'csv_file=@/path/to/phones.csv' \
  -F 'phone_columns=["Phone_1", "Phone_2"]' \
  -F 'label_column=Name'

# Option 3: JSON phone list (no CSV)
curl -X POST 'https://tracerfy.com/v1/api/dnc/scrub/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"phones": ["5125550100", "5125550101", "5125550102"]}'
```

### Example Response (200)

```json
{
  "message": "DNC scrub started",
  "dnc_queue_id": 5,
  "created_at": "2025-01-15T09:30:00Z",
  "status": "pending",
  "phones_to_check": 150,
  "credits_per_phone": 1
}
```

---

## POST DNC Scrub from Trace

`POST /v1/api/dnc/scrub-from-queue/`

Extract phone numbers from a completed trace queue from your skip tracing results and submit them for DNC scrubbing. Optionally specify which phone columns to include. Phones are deduplicated across all selected columns. 1 credit per phone checked.

**Valid phone_columns:** primary_phone, mobile_1, mobile_2, mobile_3, mobile_4, mobile_5, landline_1, landline_2, landline_3
If phone_columns is omitted, all 9 phone fields are included by default.

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |
| `Content-Type` | `application/json` |

### Parameters

| Name | In | Type | Required | Description |
|------|-----|------|----------|-------------|
| `queue_id` | body | `integer` | Yes | ID of a completed trace queue to extract phones from. |
| `phone_columns` | body | `array[string]` | No | List of phone field names to include. Defaults to all 9 phone fields. |

### Example Request

```bash
curl -X POST 'https://tracerfy.com/v1/api/dnc/scrub-from-queue/' \
  -H 'Authorization: Bearer <YOUR_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"queue_id": 37360, "phone_columns": ["primary_phone", "mobile_1", "mobile_2"]}'
```

### Example Response (200)

```json
{
  "message": "DNC scrub started",
  "dnc_queue_id": 8,
  "created_at": "2025-01-15T10:00:00Z",
  "source_queue_id": 37360,
  "status": "pending",
  "phones_to_check": 23,
  "phone_columns_used": [
    "primary_phone",
    "mobile_1",
    "mobile_2"
  ],
  "credits_per_phone": 1
}
```

---

## GET Fetch DNC Queue

`GET /v1/api/dnc/queue/:id`

Retrieve the status and results of a DNC scrub job. When complete, two download URLs are provided: download_url (all phones with DNC flags) and clean_download_url (only phones with no DNC flags). CSV columns: phone, label, national_dnc, state_dnc, dma, litigator, phone_type, is_clean.

**Note:** While the queue is still pending, the fields `phones_checked`, `phones_clean`, and `credits_deducted` are omitted from the response. They appear once the scrub completes.

### Headers

| Name | Value |
|------|-------|
| `Authorization` | `Bearer <YOUR_TOKEN>` |

### Parameters

| Name | In | Type | Required | Description |
|------|-----|------|----------|-------------|
| `:id` | path | `integer` | Yes | DNC Queue ID |

### Example Request

```bash
curl -X GET 'https://tracerfy.com/v1/api/dnc/queue/5' -H 'Authorization: Bearer <YOUR_TOKEN>'
```

### Example Response (200)

```json
{
  "id": 5,
  "created_at": "2025-01-15T09:30:00Z",
  "pending": false,
  "download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/full-results.csv",
  "clean_download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/clean-results.csv",
  "rows_uploaded": 150,
  "phones_checked": 150,
  "phones_clean": 112,
  "credits_deducted": 150,
  "source_type": "upload"
}
```

---

## POST DNC Webhooks

`POST Account.webhook_url`

When a DNC scrub completes, Tracerfy POSTs the result to the webhook URL configured in your account profile. The payload includes a `type: "dnc_scrub"` field to distinguish it from trace webhooks, plus DNC-specific fields like clean_download_url, phones_checked, and phones_clean.

### Headers

| Name | Value |
|------|-------|
| `Content-Type` | `application/json` |

### Example Request

```bash
Tracerfy sends this JSON to your Account.webhook_url when a DNC scrub completes.
```

### Example Response (200)

```json
{
  "id": 5,
  "type": "dnc_scrub",
  "created_at": "2025-01-15T09:30:00Z",
  "pending": false,
  "download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/full-results.csv",
  "clean_download_url": "https://tracerfy.nyc3.cdn.digitaloceanspaces.com/tracerfy/clean-results.csv",
  "rows_uploaded": 150,
  "phones_checked": 150,
  "phones_clean": 112,
  "credits_deducted": 150,
  "source_type": "upload"
}
```

---
