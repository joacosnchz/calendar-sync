"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ResultPage } from "@/components/ResultPage";

export default function LinkResultPage() {
  return (
    <Suspense>
      <LinkResult />
    </Suspense>
  );
}

function LinkResult() {
  const params = useSearchParams();
  const status = params.get("status");
  const email = params.get("email");
  const message = params.get("message");
  const back = { href: "/dashboard", label: "Back to your accounts" };

  if (status === "linked") {
    return (
      <ResultPage tone="good" title="Account linked" action={back}>
        <p>
          <b>{email}</b> is linked. We&apos;re copying its events into your unified calendar now; new changes will
          appear within seconds.
        </p>
      </ResultPage>
    );
  }
  if (status === "reconnected") {
    return (
      <ResultPage tone="good" title="Reconnected" action={back}>
        <p>
          <b>{email}</b> is syncing again. Any changes made while it was paused are being caught up automatically.
        </p>
      </ResultPage>
    );
  }
  if (status === "admin_approval_required") {
    return (
      <ResultPage tone="warn" title="Your IT admin needs to approve Calendar Sync" action={back}>
        <p>
          Your organization doesn&apos;t let people approve apps by themselves. Ask your IT admin to approve
          &quot;Calendar Sync&quot; (read-only access to calendars), for example with the &quot;request
          approval&quot; option on Microsoft&apos;s sign-in screen.
        </p>
        <p>Once it&apos;s approved, link the account again.</p>
        {message && <p className="text-xs text-slate-500">Details: {message}</p>}
      </ResultPage>
    );
  }
  return (
    <ResultPage tone="bad" title="That didn't work" action={back}>
      <p>{message || "Something went wrong while signing in. Please try again."}</p>
    </ResultPage>
  );
}
