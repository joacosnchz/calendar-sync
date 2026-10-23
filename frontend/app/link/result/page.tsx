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
  if (status === "consent_required") {
    return (
      <ResultPage tone="warn" title="Waiting for your IT admin" action={back}>
        <p>
          We confirmed that <b>{email}</b> is yours, but its organization hasn&apos;t approved Calendar Sync yet.
        </p>
        <p>
          Your accounts page has a one-time approval link to send to that organization&apos;s administrator. Syncing
          starts automatically once they approve.
        </p>
      </ResultPage>
    );
  }
  return (
    <ResultPage tone="bad" title="That didn't work" action={back}>
      <p>{message || "Something went wrong while signing in. Please try again."}</p>
    </ResultPage>
  );
}
