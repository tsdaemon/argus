import { expect, test, type Page } from "@playwright/test";

async function start(page: Page) {
  await page.goto("/");
  await page.getByRole("button", { name: "New conversation", exact: true }).click();
  await expect(page.getByPlaceholder("Ask argus about your home infrastructure…")).toBeVisible();
}

async function send(page: Page, message: string) {
  const input = page.getByPlaceholder("Ask argus about your home infrastructure…");
  await input.fill(message);
  await input.press("Enter");
}

test("chat survives reload, switches conversations, and continues the original", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await start(page);
  await send(page, "Check the media service");
  await expect(page.getByText("Read-only review complete: Check the media service", { exact: true })).toBeVisible();
  const first = page.url();
  await page.reload();
  await expect(page.getByText("Read-only review complete: Check the media service", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "New conversation", exact: true }).click();
  await send(page, "Check disk usage");
  await expect(page.getByText("Read-only review complete: Check disk usage", { exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("button", { name: "Check the media service" }).click();
  await expect(page).toHaveURL(first);
  await expect(page.getByText("Read-only review complete: Check the media service", { exact: true })).toBeVisible();
  await expect(page.getByText("Read-only review complete: Check disk usage", { exact: true })).toHaveCount(0);
  await send(page, "Now check its logs");
  await expect(page.getByText("Read-only review complete: Now check its logs", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("Read-only review complete: Now check its logs", { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
  await page.screenshot({ path: "/tmp/argus-chat-desktop.png" });
});

for (const decision of ["Approve", "Reject"]) {
  test(`${decision} a restored approval without executing on reload`, async ({ page, request }) => {
    const before = (await (await request.get("/__test__/operations")).json()).length;
    await start(page);
    await send(page, "Please restart example-service");
    await expect(page.getByRole("region", { name: "Tool approval" })).toBeVisible();
    await page.reload();
    const approval = page.getByRole("region", { name: "Tool approval" });
    await expect(approval).toBeVisible();
    expect((await (await request.get("/__test__/operations")).json()).length).toBe(before);
    await page.screenshot({ path: `/tmp/argus-approval-${decision}.png` });
    await approval.getByRole("button", { name: decision, exact: true }).click();
    await approval.getByRole("button", { name: "Submit decisions" }).click();
    await expect(page.getByText(/Operation reviewed\./).first()).toBeVisible();
    await expect(approval).toHaveCount(0);
    expect((await (await request.get("/__test__/operations")).json()).length).toBe(before + (decision === "Approve" ? 1 : 0));
    await page.reload();
    await expect(page.getByText(/Operation reviewed\./).first()).toBeVisible();
    await expect(approval).toHaveCount(0);
  });
}

test("batch approval requires a decision for each action", async ({ page, request }) => {
  const before = (await (await request.get("/__test__/operations")).json()).length;
  await start(page);
  await send(page, "Please restart both services");
  const approval = page.getByRole("region", { name: "Tool approval" });
  await expect(approval).toBeVisible();
  const submit = approval.getByRole("button", { name: "Submit decisions" });
  await expect(submit).toBeDisabled();
  await approval.getByRole("button", { name: "Approve", exact: true }).first().click();
  await expect(submit).toBeDisabled();
  await approval.getByRole("button", { name: "Reject", exact: true }).nth(1).click();
  await submit.click();
  await expect(page.getByText(/Operation reviewed\./).first()).toBeVisible();
  expect((await (await request.get("/__test__/operations")).json()).length).toBe(before + 1);
});

test("mobile chat fits the viewport and history failures can be retried", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/threads?*", (route) => route.fulfill({ status: 503 }));
  await page.goto("/");
  await expect(page.getByRole("alert")).toBeVisible();
  await page.unroute("**/api/threads?*");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "New conversation", exact: true }).click();
  await send(page, "Check the NAS");
  await expect(page.getByText("Read-only review complete: Check the NAS", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: "/tmp/argus-chat-mobile.png" });
});

test("sidebar collapses to a rail and the choice survives a reload", async ({ page }) => {
  await start(page);
  const toggle = page.getByRole("button", { name: "Collapse sidebar" });
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await toggle.click();
  await expect(page.getByRole("navigation")).toBeHidden();
  await expect(page.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute("aria-expanded", "false");
  await page.reload();
  await expect(page.getByRole("navigation")).toBeHidden();
  await page.getByRole("button", { name: "Expand sidebar" }).click();
  await expect(page.getByRole("navigation")).toBeVisible();
});

test("a conversation can be deleted after confirming, and stays deleted", async ({ page }) => {
  await start(page);
  await send(page, "Delete me please");
  await expect(page.getByText("Read-only review complete: Delete me please", { exact: true })).toBeVisible();
  const row = page.locator(".thread-row", { hasText: "Delete me please" });
  const confirm = page.getByRole("group", { name: "Confirm deletion" });

  await row.getByRole("button", { name: "Delete conversation" }).click();
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(row).toHaveCount(1);

  await row.getByRole("button", { name: "Delete conversation" }).click();
  await confirm.getByRole("button", { name: "Delete", exact: true }).click();
  await expect(page.locator(".thread-row", { hasText: "Delete me please" })).toHaveCount(0);
  await expect(page.getByText("Read-only review complete: Delete me please", { exact: true })).toHaveCount(0);

  await page.reload();
  await expect(page.locator(".thread-row", { hasText: "Delete me please" })).toHaveCount(0);
});

test("a conversation can be renamed, and the title stays", async ({ page }) => {
  await start(page);
  await send(page, "Name me later");
  await expect(page.getByText("Read-only review complete: Name me later", { exact: true })).toBeVisible();
  const row = page.locator(".thread-row", { hasText: "Name me later" });

  await row.getByRole("button", { name: "Rename conversation" }).click();
  await page.getByRole("textbox", { name: "Conversation title" }).press("Escape");
  await expect(row).toHaveCount(1);

  await row.getByRole("button", { name: "Rename conversation" }).click();
  await page.getByRole("textbox", { name: "Conversation title" }).fill("Router uptime notes");
  await page.getByRole("textbox", { name: "Conversation title" }).press("Enter");
  const renamed = page.locator(".thread-row", { hasText: "Router uptime notes" });
  await expect(renamed).toHaveCount(1);

  await send(page, "One more question");
  await expect(page.getByText("Read-only review complete: One more question", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator(".thread-row", { hasText: "Router uptime notes" })).toHaveCount(1);
});

test("messages show their day once and their time, also after a reload", async ({ page }) => {
  await start(page);
  await send(page, "First timed question");
  await expect(page.getByText("Read-only review complete: First timed question", { exact: true })).toBeVisible();
  await send(page, "Second timed question");
  await expect(page.getByText("Read-only review complete: Second timed question", { exact: true })).toBeVisible();
  for (const _ of [0, 1]) {
    await expect(page.getByRole("separator").filter({ hasText: "Today" })).toHaveCount(1);
    await expect(page.locator(".message-time")).toHaveCount(4);
    await expect(page.locator(".message-time").first()).toHaveText(/^\d{1,2}:\d{2}/);
    await page.reload();
  }
});

test("a tool call shows its key arguments and classification", async ({ page }) => {
  await start(page);
  await send(page, "What is the router uptime?");
  const card = page.locator(".tool-call").filter({ hasText: "ssh_run" });
  await expect(card.getByText("router $ uptime")).toBeVisible();
  await expect(card.locator(".tool-call-risk")).toHaveText("read");
  await card.getByRole("button").click();
  await expect(card.getByText("fixed: read 1.00")).toBeVisible();
  const result = card.locator("pre").last();
  await expect(result).toContainText("exit 0");
  await expect(result).not.toContainText("[risk:");
  await card.screenshot({ path: "/tmp/argus-tool-call.png" });
});

test("attachments: images go as images, text files are inlined as text", async ({ page }) => {
  await start(page);
  await page.locator('input[type="file"]').setInputFiles([
    { name: "router.log", mimeType: "", buffer: Buffer.from("wl0: link down\nwl0: link up\n") },
    { name: "shot.png", mimeType: "image/png", buffer: Buffer.from("89504e470d0a1a0a", "hex") },
  ]);
  await expect(page.getByText("router.log")).toBeVisible();
  const sent = page.waitForRequest((r) => r.url().endsWith("/agent") && r.method() === "POST");
  await send(page, "What happened on the router?");
  const body = (await sent).postDataJSON();
  const parts = body.messages[0].content as { type: string; text?: string; source?: { mimeType: string } }[];
  expect(parts.map((p) => p.type)).toEqual(["text", "text", "image"]);
  expect(parts[1].text).toContain("Attached file router.log:");
  expect(parts[1].text).toContain("wl0: link down");
  expect(parts[2].source?.mimeType).toBe("image/png");
  await expect(page.getByText(/Read-only review complete/)).toBeVisible();
});

test("a long conversation renders every message instead of virtualizing", async ({ page }) => {
  await start(page);
  for (let i = 1; i <= 26; i++) {
    await send(page, `Question ${i}`);
    await expect(page.getByText(`Read-only review complete: Question ${i}`, { exact: true })).toBeVisible();
  }
  await expect(page.locator("[data-message-id]").first()).toContainText("Question 1");
  await expect(page.locator("[data-testid=copilot-message-list] [data-index]")).toHaveCount(0);
});


test("external sender remains visible after reload and an operator reply", async ({ page, request }) => {
  const seeded = await (await request.post("/__test__/external-message")).json();
  await page.goto(`/?thread=${seeded.id}`);
  await expect(page.locator(".message-author")).toHaveText("Agent hermes");
  await expect(page.getByText("Check the storage pool", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator(".message-author")).toHaveText("Agent hermes");
  await send(page, "Now check its logs");
  await expect(page.getByText("Read-only review complete: Now check its logs", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.locator(".message-author")).toHaveCount(1);
  await expect(page.locator(".message-author")).toHaveText("Agent hermes");
});


test("conversation tabs separate operator and agent threads", async ({ page, request }) => {
  await request.post("/__test__/external-message");
  await start(page);
  await send(page, "My private diagnostics");
  await expect(page.getByText("Read-only review complete: My private diagnostics", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Agents", exact: true }).click();
  await expect(page.getByRole("navigation").getByText("My private diagnostics", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("navigation").getByText("Agent · hermes").first()).toBeVisible();
  await expect(page.locator(".external-message").first()).toBeVisible();
  await page.getByRole("tab", { name: "Mine", exact: true }).click();
  await expect(page.getByRole("navigation").getByText("My private diagnostics", { exact: true })).toBeVisible();
  await expect(page.getByRole("navigation").getByText("Agent · hermes")).toHaveCount(0);
  await page.getByRole("tab", { name: "All", exact: true }).click();
  await expect(page.getByRole("navigation").getByText("Agent · hermes").first()).toBeVisible();
});

test("a deep link to an agent thread opens it from the default tab", async ({ page, request }) => {
  const { id } = await (await request.post("/__test__/external-message")).json();
  await page.goto(`/?thread=${id}`);
  await expect(page.getByRole("tab", { name: "All", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".external-message").first()).toBeVisible();
  await expect(page.getByRole("navigation").getByText("Agent · hermes").first()).toBeVisible();
});

test("a conversation shows what its model calls cost", async ({ page }) => {
  await start(page);
  await send(page, "Price this question");
  await expect(page.getByText("Read-only review complete: Price this question", { exact: true })).toBeVisible();
  const row = page.getByRole("navigation").getByRole("button", { name: /Price this question/ });
  await expect(row.locator(".thread-cost")).toHaveText("$0.004");
  await expect(page.locator(".sidebar-footer .spent")).toHaveText(/^\$[\d.]+ spent$/);
  await page.screenshot({ path: "/tmp/argus-cost.png" });
});
