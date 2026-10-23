"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ResultPage } from "@/components/ResultPage";

// Shown to customer tenant admins after they act on an admin consent link.
export default function ConsentResultPage() {
  return (
    <Suspense>
      <ConsentResult />
    </Suspense>
  );
}

function ConsentResult() {
  const params = useSearchParams();
  const status = params.get("status");

  if (status === "connected") {
    return (
      <ResultPage tone="good" title={`${params.get("tenant") ?? "Your organization"} is connected`}>
        <p>Thanks! People in your organization can now link the calendars they own.</p>
        <p>
          Calendar Sync has read-only access to calendars (<code>Calendars.Read</code>). To limit which mailboxes it
          can read, apply an Exchange application access policy or RBAC for Applications to its service principal.
          You can revoke access at any time by deleting the &quot;Calendar Sync&quot; Enterprise application.
        </p>
      </ResultPage>
    );
  }
  if (status === "denied") {
    return (
      <ResultPage tone="warn" title="Consent was not granted">
        <p>No access was given. {params.get("message")}</p>
      </ResultPage>
    );
  }
  return (
    <ResultPage tone="bad" title="Something went wrong">
      <p>{params.get("message") || "The consent request could not be completed."}</p>
    </ResultPage>
  );
}
