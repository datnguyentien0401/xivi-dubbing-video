import { Badge } from "@/components/ui/badge";

interface SafetyBadgeProps {
  status: "pending" | "ok" | "flagged";
  reasoning?: string | null;
}

/** Safety status badge — ALWAYS shows the natural-language reasoning when
 * flagged (see docs/frontend-spec.md section 5 — surfacing Gen AI reasoning). */
export function SafetyBadge({ status, reasoning }: SafetyBadgeProps) {
  const variant =
    status === "ok"
      ? "success"
      : status === "flagged"
        ? "warning"
        : "secondary";

  return (
    <Badge variant={variant} title={reasoning ?? undefined}>
      {status === "ok"
        ? "Safe"
        : status === "flagged"
          ? "Needs Review"
          : "Checking..."}
    </Badge>
  );
}
