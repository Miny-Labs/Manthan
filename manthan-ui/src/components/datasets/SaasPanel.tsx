import { useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { ChevronRight, Check, AlertTriangle, Loader2 } from "lucide-react";
import { BASE_URL } from "@/api/client";
import { ConnectorIcon } from "./ConnectorIcon";
import { cn } from "@/lib/utils";

/**
 * SaaS connector picker — per-connector input forms.
 *
 * Each of the ten connectors expands inline to its own field schema
 * (API key, OAuth token, resource selector). Submit posts to
 * `POST /datasets/connect-saas` with `{connector, config}`. The backend
 * fetches the requested resource via the upstream REST API, flattens
 * the response with pandas.json_normalize, trims to the useful
 * business columns, and routes the result through Manthan's standard
 * ingestion pipeline so the new dataset lands as Gold state with a
 * DCD, rollups, click-to-audit metrics, the works.
 */

type ConnectorSlug =
  | "stripe"
  | "hubspot"
  | "salesforce"
  | "shopify"
  | "notion"
  | "airtable"
  | "googleads"
  | "meta"
  | "github"
  | "slack";

type FieldKind = "text" | "password" | "select";

interface FieldDef {
  name: string;
  label: string;
  kind: FieldKind;
  placeholder?: string;
  options?: string[];
  helper?: string;
  required?: boolean;
}

interface ConnectorDef {
  slug: ConnectorSlug;
  label: string;
  blurb: string;
  status: "live" | "preview";
  fields: FieldDef[];
}

const CONNECTORS: ConnectorDef[] = [
  {
    slug: "github",
    label: "GitHub",
    blurb: "Issues, pull requests, commits, releases, contributors.",
    status: "live",
    fields: [
      {
        name: "repo",
        label: "Repository",
        kind: "text",
        placeholder: "owner/repo · e.g. duckdb/duckdb",
        required: true,
      },
      {
        name: "resource",
        label: "Data",
        kind: "select",
        options: ["issues", "pull_requests", "commits", "releases", "contributors"],
        required: true,
      },
      {
        name: "token",
        label: "Personal access token",
        kind: "password",
        placeholder: "ghp_… (leave blank for public repos)",
        helper: "Optional. Public repos work unauthenticated, but you'll hit GitHub's 60/hr rate limit.",
      },
    ],
  },
  {
    slug: "stripe",
    label: "Stripe",
    blurb: "Charges, customers, invoices, subscriptions.",
    status: "live",
    fields: [
      {
        name: "api_key",
        label: "Secret API key",
        kind: "password",
        placeholder: "sk_live_… or sk_test_…",
        required: true,
      },
      {
        name: "resource",
        label: "Resource",
        kind: "select",
        options: ["charges", "customers", "invoices", "subscriptions", "balance_transactions"],
        required: true,
      },
      {
        name: "lookback",
        label: "Lookback window",
        kind: "select",
        options: ["7 days", "30 days", "90 days", "12 months"],
      },
    ],
  },
  {
    slug: "hubspot",
    label: "HubSpot",
    blurb: "Contacts, companies, deals, tickets.",
    status: "live",
    fields: [
      {
        name: "access_token",
        label: "Private-app access token",
        kind: "password",
        placeholder: "pat-na1-…",
        required: true,
      },
      {
        name: "resource",
        label: "Object",
        kind: "select",
        options: ["contacts", "companies", "deals", "tickets"],
        required: true,
      },
    ],
  },
  {
    slug: "salesforce",
    label: "Salesforce",
    blurb: "Accounts, Contacts, Opportunities, Leads.",
    status: "live",
    fields: [
      {
        name: "instance_url",
        label: "Instance URL",
        kind: "text",
        placeholder: "https://yourorg.my.salesforce.com",
        required: true,
      },
      {
        name: "username",
        label: "Username",
        kind: "text",
        required: true,
      },
      {
        name: "password",
        label: "Password",
        kind: "password",
        required: true,
      },
      {
        name: "security_token",
        label: "Security token",
        kind: "password",
        helper: "From Setup → My Personal Information → Reset My Security Token.",
        required: true,
      },
      {
        name: "object",
        label: "SObject",
        kind: "select",
        options: ["Account", "Contact", "Opportunity", "Lead", "Case"],
        required: true,
      },
    ],
  },
  {
    slug: "shopify",
    label: "Shopify",
    blurb: "Orders, customers, products, inventory.",
    status: "live",
    fields: [
      {
        name: "shop_domain",
        label: "Shop domain",
        kind: "text",
        placeholder: "mystore.myshopify.com",
        required: true,
      },
      {
        name: "access_token",
        label: "Admin API access token",
        kind: "password",
        placeholder: "shpat_…",
        required: true,
      },
      {
        name: "resource",
        label: "Resource",
        kind: "select",
        options: ["orders", "customers", "products", "inventory_items"],
        required: true,
      },
    ],
  },
  {
    slug: "notion",
    label: "Notion",
    blurb: "Database rows + page metadata.",
    status: "live",
    fields: [
      {
        name: "integration_token",
        label: "Internal integration token",
        kind: "password",
        placeholder: "secret_…",
        required: true,
      },
      {
        name: "database_id",
        label: "Database ID",
        kind: "text",
        placeholder: "32-char hex from the database URL",
        required: true,
      },
    ],
  },
  {
    slug: "airtable",
    label: "Airtable",
    blurb: "Bases, tables, views.",
    status: "live",
    fields: [
      {
        name: "access_token",
        label: "Personal access token",
        kind: "password",
        placeholder: "pat… (from airtable.com/create/tokens)",
        required: true,
      },
      {
        name: "base_id",
        label: "Base ID",
        kind: "text",
        placeholder: "app…",
        required: true,
      },
      {
        name: "table",
        label: "Table name",
        kind: "text",
        required: true,
      },
    ],
  },
  {
    slug: "googleads",
    label: "Google Ads",
    blurb: "Campaigns, ad groups, keyword performance.",
    status: "live",
    fields: [
      {
        name: "customer_id",
        label: "Customer ID",
        kind: "text",
        placeholder: "123-456-7890",
        required: true,
      },
      {
        name: "developer_token",
        label: "Developer token",
        kind: "password",
        required: true,
      },
      {
        name: "client_id",
        label: "OAuth client ID",
        kind: "text",
        placeholder: "….apps.googleusercontent.com",
        required: true,
      },
      {
        name: "client_secret",
        label: "OAuth client secret",
        kind: "password",
        required: true,
      },
      {
        name: "refresh_token",
        label: "OAuth refresh token",
        kind: "password",
        helper: "Generate via the Google OAuth 2.0 Playground with the AdWords scope.",
        required: true,
      },
      {
        name: "resource",
        label: "Report",
        kind: "select",
        options: ["campaign_performance", "ad_group_performance", "keyword_view", "search_terms"],
        required: true,
      },
    ],
  },
  {
    slug: "meta",
    label: "Meta Ads",
    blurb: "Campaigns, ads, insights from Facebook + Instagram.",
    status: "live",
    fields: [
      {
        name: "access_token",
        label: "Long-lived access token",
        kind: "password",
        placeholder: "EAAG…",
        required: true,
      },
      {
        name: "ad_account_id",
        label: "Ad account ID",
        kind: "text",
        placeholder: "act_…",
        required: true,
      },
      {
        name: "resource",
        label: "Resource",
        kind: "select",
        options: ["campaigns", "adsets", "ads", "insights"],
        required: true,
      },
    ],
  },
  {
    slug: "slack",
    label: "Slack",
    blurb: "Channels, messages, members.",
    status: "live",
    fields: [
      {
        name: "bot_token",
        label: "Bot user OAuth token",
        kind: "password",
        placeholder: "xoxb-…",
        required: true,
      },
      {
        name: "channel_id",
        label: "Channel ID",
        kind: "text",
        placeholder: "C01ABCDEF (leave blank for all channels)",
      },
      {
        name: "resource",
        label: "Resource",
        kind: "select",
        options: ["messages", "channels", "members", "reactions"],
        required: true,
      },
    ],
  },
];

type SubmitState =
  | { kind: "idle" }
  | { kind: "submitting" }
  | { kind: "ok"; message: string }
  | { kind: "error"; message: string };

export function SaasPanel() {
  const [openSlug, setOpenSlug] = useState<ConnectorSlug | null>(null);
  return (
    <div>
      <p className="text-sm text-text-secondary mb-4 font-body">
        SaaS connectors land in your workspace as standard datasets. Pick one
        to enter credentials, choose a resource, and start syncing.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {CONNECTORS.map((c) => (
          <ConnectorCard
            key={c.slug}
            connector={c}
            open={openSlug === c.slug}
            onToggle={() => setOpenSlug(openSlug === c.slug ? null : c.slug)}
          />
        ))}
      </div>
      <p className="text-[11px] text-text-tertiary mt-4">
        Custom connector? Paste an OpenAPI spec URL and we'll scaffold one for
        you via <code className="font-mono">dlt-init-openapi</code>.
      </p>
    </div>
  );
}

function ConnectorCard({
  connector,
  open,
  onToggle,
}: {
  connector: ConnectorDef;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border bg-surface-1 transition-all overflow-hidden",
        open ? "border-accent" : "border-border hover:border-border-strong",
      )}
    >
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-3 py-2.5 text-left"
      >
        <ConnectorIcon slug={connector.slug} size={20} showBackground />
        <div className="flex-1 min-w-0">
          <div className="text-sm text-text-primary font-body">{connector.label}</div>
          <div className="text-[11px] text-text-tertiary truncate">{connector.blurb}</div>
        </div>
        {connector.status === "live" ? (
          <span className="text-[10px] text-emerald-700 dark:text-emerald-300 bg-emerald-100 dark:bg-emerald-950/40 px-1.5 py-0.5 rounded uppercase tracking-wider shrink-0">
            Live
          </span>
        ) : (
          <span className="text-[10px] text-text-tertiary uppercase tracking-wider shrink-0">
            Preview
          </span>
        )}
        <ChevronRight
          size={14}
          className={cn(
            "text-text-faint shrink-0 transition-transform",
            open && "rotate-90",
          )}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="form"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <ConnectorForm connector={connector} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function ConnectorForm({ connector }: { connector: ConnectorDef }) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      connector.fields.map((f) => [
        f.name,
        f.kind === "select" && f.options ? f.options[0] : "",
      ]),
    ),
  );
  const [state, setState] = useState<SubmitState>({ kind: "idle" });

  const update = (name: string, value: string) =>
    setValues((v) => ({ ...v, [name]: value }));

  const missingRequired = connector.fields
    .filter((f) => f.required && !values[f.name]?.trim())
    .map((f) => f.label);

  async function submit() {
    if (missingRequired.length > 0) {
      setState({
        kind: "error",
        message: `Missing required: ${missingRequired.join(", ")}`,
      });
      return;
    }
    setState({ kind: "submitting" });
    try {
      const r = await fetch(`${BASE_URL}/datasets/connect-saas`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          connector: connector.slug,
          config: values,
        }),
      });
      const body = await r.json();
      if (r.ok) {
        setState({
          kind: "ok",
          message:
            body.message ||
            `Connected. ${body.dataset_id ? "Dataset ready: " + body.dataset_id : "Sync scheduled."}`,
        });
      } else {
        setState({
          kind: "error",
          message: body.detail || body.message || `HTTP ${r.status}`,
        });
      }
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : "Network error",
      });
    }
  }

  return (
    <div className="px-3 pb-3 pt-1 border-t border-border bg-surface-0 space-y-3">
      {connector.fields.map((f) => (
        <FieldInput key={f.name} field={f} value={values[f.name]} onChange={(v) => update(f.name, v)} />
      ))}
      <div className="flex items-center justify-between pt-1">
        <div className="text-[11px] text-text-tertiary truncate pr-2">
          {state.kind === "error" && (
            <span className="text-rose-600 dark:text-rose-300 flex items-center gap-1">
              <AlertTriangle size={11} /> {state.message}
            </span>
          )}
          {state.kind === "ok" && (
            <span className="text-emerald-700 dark:text-emerald-300 flex items-center gap-1">
              <Check size={11} /> {state.message}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={submit}
          disabled={state.kind === "submitting"}
          className="text-xs px-3 py-1.5 rounded-md bg-accent text-on-accent hover:bg-accent-hover transition-colors disabled:opacity-60 flex items-center gap-1.5 shrink-0"
        >
          {state.kind === "submitting" && <Loader2 size={11} className="animate-spin" />}
          {state.kind === "submitting" ? "Connecting…" : `Connect ${connector.label}`}
        </button>
      </div>
    </div>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: FieldDef;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="block text-[11px] text-text-secondary font-body mb-1">
        {field.label}
        {field.required && <span className="text-rose-500 ml-1">*</span>}
      </label>
      {field.kind === "select" ? (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full text-sm bg-surface-1 border border-border rounded-md px-2.5 py-1.5 font-body text-text-primary focus:border-accent focus:outline-none"
        >
          {field.options?.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      ) : (
        <input
          type={field.kind === "password" ? "password" : "text"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          className="w-full text-sm bg-surface-1 border border-border rounded-md px-2.5 py-1.5 font-mono text-text-primary placeholder:text-text-faint focus:border-accent focus:outline-none"
        />
      )}
      {field.helper && (
        <p className="text-[10px] text-text-tertiary mt-1 font-body">{field.helper}</p>
      )}
    </div>
  );
}
