import { useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Check, AlertTriangle, Loader2 } from "lucide-react";
import { BASE_URL } from "@/api/client";
import { ConnectorIcon } from "./ConnectorIcon";
import { cn } from "@/lib/utils";

/**
 * SaaS connector picker — two-pane layout.
 *
 * Left rail lists every connector; right pane is the form for the
 * selected one. Submit posts to `POST /datasets/connect-saas` with
 * `{connector, config}`. The backend fetches the requested resource
 * via the upstream REST API, flattens the response with
 * pandas.json_normalize, trims to the useful business columns, and
 * routes the result through Manthan's standard ingestion pipeline so
 * the new dataset lands as Gold state — DCD, rollups, click-to-audit.
 */

type ConnectorSlug =
  | "github"
  | "stripe"
  | "hubspot"
  | "salesforce"
  | "shopify"
  | "notion"
  | "airtable"
  | "googleads"
  | "meta"
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
  fields: FieldDef[];
}

const CONNECTORS: ConnectorDef[] = [
  {
    slug: "github",
    label: "GitHub",
    blurb: "Issues, pull requests, commits, releases, contributors.",
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
        helper:
          "Optional. Public repos work unauthenticated, but you'll hit GitHub's 60/hr rate limit.",
      },
    ],
  },
  {
    slug: "stripe",
    label: "Stripe",
    blurb: "Charges, customers, invoices, subscriptions.",
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
        options: [
          "charges",
          "customers",
          "invoices",
          "subscriptions",
          "balance_transactions",
        ],
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
    fields: [
      {
        name: "instance_url",
        label: "Instance URL",
        kind: "text",
        placeholder: "https://yourorg.my.salesforce.com",
        required: true,
      },
      { name: "username", label: "Username", kind: "text", required: true },
      { name: "password", label: "Password", kind: "password", required: true },
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
      { name: "table", label: "Table name", kind: "text", required: true },
    ],
  },
  {
    slug: "googleads",
    label: "Google Ads",
    blurb: "Campaigns, ad groups, keyword performance.",
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
        helper:
          "Generate via the Google OAuth 2.0 Playground with the AdWords scope.",
        required: true,
      },
      {
        name: "resource",
        label: "Report",
        kind: "select",
        options: [
          "campaign_performance",
          "ad_group_performance",
          "keyword_view",
          "search_terms",
        ],
        required: true,
      },
    ],
  },
  {
    slug: "meta",
    label: "Meta Ads",
    blurb: "Campaigns, ads, insights from Facebook + Instagram.",
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
  const [activeSlug, setActiveSlug] = useState<ConnectorSlug>(CONNECTORS[0].slug);
  const active = CONNECTORS.find((c) => c.slug === activeSlug) ?? CONNECTORS[0];

  return (
    <div className="-mx-5 -my-5 grid grid-cols-[200px_1fr] min-h-[420px]">
      {/* Left rail — connector list */}
      <nav className="border-r border-border bg-surface-1/40 overflow-y-auto py-2">
        {CONNECTORS.map((c) => {
          const isActive = c.slug === activeSlug;
          return (
            <button
              key={c.slug}
              type="button"
              onClick={() => setActiveSlug(c.slug)}
              className={cn(
                "w-full flex items-center gap-2.5 px-4 py-2 text-sm text-left transition-colors font-body",
                isActive
                  ? "bg-surface-0 text-text-primary"
                  : "text-text-secondary hover:bg-surface-sunken hover:text-text-primary",
              )}
            >
              <ConnectorIcon slug={c.slug} size={16} showBackground={false} />
              <span className="flex-1 truncate">{c.label}</span>
              {isActive && (
                <span className="w-1 h-4 rounded-sm bg-accent shrink-0" aria-hidden />
              )}
            </button>
          );
        })}
      </nav>

      {/* Right pane — selected connector form */}
      <div className="overflow-y-auto px-6 py-5 font-body">
        <AnimatePresence mode="wait">
          <motion.div
            key={active.slug}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12 }}
          >
            <ConnectorForm connector={active} />
          </motion.div>
        </AnimatePresence>
        <p className="text-[11px] text-text-tertiary mt-6 pt-4 border-t border-border">
          Custom connector? Paste an OpenAPI spec URL and we'll scaffold one for
          you via <code className="font-mono">dlt-init-openapi</code>.
        </p>
      </div>
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

  async function submit() {
    const missing = connector.fields
      .filter((f) => f.required && !values[f.name]?.trim())
      .map((f) => f.label);
    if (missing.length > 0) {
      setState({
        kind: "error",
        message: `Missing required: ${missing.join(", ")}`,
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
            `Connected. ${body.dataset_id ? "Dataset ready: " + body.dataset_id : "Sync started."}`,
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
    <div>
      <header className="flex items-center gap-3 pb-4 border-b border-border mb-5">
        <ConnectorIcon slug={connector.slug} size={28} showBackground />
        <div className="min-w-0">
          <h3 className="text-base text-text-primary font-display">
            {connector.label}
          </h3>
          <p className="text-xs text-text-tertiary truncate">{connector.blurb}</p>
        </div>
      </header>

      <div className="space-y-3.5">
        {connector.fields.map((f) => (
          <FieldInput
            key={f.name}
            field={f}
            value={values[f.name]}
            onChange={(v) => update(f.name, v)}
          />
        ))}
      </div>

      <div className="flex items-center justify-between gap-3 mt-5 pt-4 border-t border-border">
        <div className="text-[12px] text-text-tertiary min-w-0 flex-1">
          {state.kind === "error" && (
            <span className="text-rose-600 dark:text-rose-300 flex items-center gap-1.5">
              <AlertTriangle size={12} /> {state.message}
            </span>
          )}
          {state.kind === "ok" && (
            <span className="text-emerald-700 dark:text-emerald-300 flex items-center gap-1.5">
              <Check size={12} /> {state.message}
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={submit}
          disabled={state.kind === "submitting"}
          className="text-sm px-4 py-2 rounded-md bg-accent text-on-accent hover:bg-accent-hover transition-colors disabled:opacity-60 flex items-center gap-2 shrink-0 font-body"
        >
          {state.kind === "submitting" && (
            <Loader2 size={12} className="animate-spin" />
          )}
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
        <p className="text-[10px] text-text-tertiary mt-1 font-body leading-snug">
          {field.helper}
        </p>
      )}
    </div>
  );
}
