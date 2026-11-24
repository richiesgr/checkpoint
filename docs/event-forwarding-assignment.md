# Home Assignment: Event Forwarding (Tech Lead)

## About this assignment

You'll design and build a small version of an **Event Forwarding** product, and write a short design document explaining your decisions.

- **Time:** plan for about **5 hours**. The scope is intentionally larger than that. We want to see what you prioritize, what you cut, and why. A small, working core with a strong design document is better than broad, unfinished code.
- **AI tools:** allowed and expected. Please include your full AI interaction log with your submission (see "What to submit"). In the follow-up session you'll walk us through your code and design, and we'll change some requirements live. Make sure you understand and can defend everything you submit.
- **Environment:** local only. No cloud account is needed. Docker is fine.

## The product

Several products (for example *sales*, *security*, *identity*) emit events on behalf of many customers (tenants). Customers want those events **forwarded to systems they own**, such as their SIEM, their data lake, or a webhook, shaped the way they need them.

Customers manage this themselves in a self-service portal. For each combination of product, event schema and tenant, they decide:

- which events to forward, and which to skip
- which payload fields to include
- extra fields to add, either constant values (e.g. `region = "eu"`) or values calculated from the payload (e.g. `risk_score = risk * 2`)
- where to send the events, and in what format

Customers can change their configuration at any time.

Our landing zone receives the events of **every product, for every tenant**. A tenant typically forwards only a few of those products. For example, tenant A may have ten products landing but choose to forward only *sales* and *security*.

### Destinations

The product supports three kinds of destination. Design for all three; implement at least one end to end.

1. **File destination.** JSON Lines or Parquet files written to a tenant-specific location. Locally, a directory represents the customer's storage bucket.
2. **HTTP destination.** Events are POSTed to a customer-provided URL. Locally, you can write a trivial mock endpoint.
3. **Syslog destination.** Many customers' SIEMs only accept syslog. Events are sent as syslog messages to a customer-provided host and port. Locally, a trivial mock receiver is enough.

Destinations are owned and operated by customers. We don't control their availability, capacity or response times.

### Data isolation

The most serious incident this product can cause is **forwarding data a customer must not receive**. That means one tenant's events reaching another tenant's destination, or a tenant receiving events from a product they didn't choose to forward.

Show us how your solution prevents this. Cover the design, the code, and the tests, and explain how you would convince a security reviewer that it holds.

### Current scale

These figures are illustrative, but use them in your reasoning:

| | Today | Expected in 18 months |
|---|---|---|
| Ingested volume | ~2 TB/day | ~6 TB/day |
| Peak rate | ~40k events/sec | ~120k events/sec |
| Tenants | ~5,000 | ~15,000 |
| Distribution | Top 10 tenants ≈ 60% of volume; most tenants < 1 GB/day | similar |
| Products landing per tenant | 10–30 | more |
| Products a tenant forwards | typically 2–5 | similar |
| Destinations per tenant | 1–5 | similar |
| Expected delivery latency | events reach the destination within ~5 minutes | same |

Our data platform runs on **Apache Spark**. Use Spark where it fits. **Where it doesn't fit, say so and use something else.** Either way, justify the choice.

## Input

Events arrive as JSON Lines files in an input directory (representing a landing zone). Each event has this envelope:

```json
{
  "event_id": "e1",
  "product": "sales",
  "schema": "purchase",
  "tenant_id": "3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90",
  "data": { "order_id": "o1", "amount": 10 }
}
```

`tenant_id` is a UUID. `data` is a JSON object whose fields depend on the product and schema.

Provide your own sample input. Minimal acceptance scenario, where tenant A is `3f2b8c1e-…` and tenant B is `9d4e1a72-…`:

```jsonl
{"event_id":"e1","product":"sales","schema":"purchase","tenant_id":"3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90","data":{"order_id":"o1","amount":10,"internal_note":"private"}}
{"event_id":"e2","product":"security","schema":"alert","tenant_id":"3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90","data":{"alert_id":"a1","risk":7}}
{"event_id":"e3","product":"sales","schema":"purchase","tenant_id":"9d4e1a72-3c5b-4f8e-a1d2-6b7c8e9f0a13","data":{"order_id":"o2","amount":5}}
{"event_id":"e4","product":"sales","schema":"purchase","tenant_id":"","data":{"order_id":"o3","amount":3}}
{"event_id":"e5","product":"identity","schema":"login","tenant_id":"3f2b8c1e-7a4d-4e9b-9c1a-2d5e6f7a8b90","data":{"user":"u1","result":"success"}}
```

With your sample configuration, where tenant A forwards *sales* and *security* but not *identity*:

- `e1` and `e2` reach tenant A's file destination, and `internal_note` is not forwarded.
- `e3` reaches tenant B's HTTP or syslog destination.
- `e4` and `e5` never reach any customer.

Beyond this scenario, you decide the configuration format, the output model, and the system's behaviour.

## What to submit

1. **Working code** that implements a meaningful core of the product: configuration, processing, and at least one destination type end to end. Include setup and dependency instructions with exact versions (Python, Java, Spark, etc.) and the run commands.
2. **Sample configuration and input**, including the acceptance scenario above.
3. **Tests** for the parts you consider most important. Tell us why you chose those parts.
4. **A design document, no more than 3 pages**, covering:
   - **Architecture.** The components and data flow of the *full* product, not just what you built. Where Spark is used, where it isn't, and why.
   - **Data isolation.** How the design guarantees that no tenant receives another tenant's data, or a product they didn't choose to forward. Also how this is enforced, tested and audited.
   - **Operating it.** What you expect to go wrong once this runs in production for thousands of customers, and how your design deals with it.
   - **Scale and cost.** How the design behaves at today's volume and at 3x. What breaks first, and what drives cost.
   - **Scope.** What you built, what you cut and why, and what you'd do next with another week.
5. **A README** that gets us from clone to a working run and to inspecting the output.
6. **Your AI interaction log.** The complete, unedited history of the prompts you gave AI tools and their responses, across every tool you used. Exported chats, agent session transcripts, or a single file per tool are all fine. Remove any secrets or personal information, but don't curate it otherwise. We use it to understand how you work with AI, not to count how much you relied on it.

## How we evaluate

- Sound technical judgment, especially around Spark: where it fits and where it doesn't.
- How rigorously the solution prevents cross-tenant and cross-product data exposure.
- How well the design holds up in real-world operation.
- Reasoning about scale, efficiency and cost.
- Correctness and quality of the code you chose to build.
- Clear prioritization and communication.

We'll discuss your submission in a follow-up session of about 75 minutes.
