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
