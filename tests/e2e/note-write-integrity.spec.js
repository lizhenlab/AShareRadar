import { expect, test } from "@playwright/test";
import { editNote, noteApi, noteForm, openNotes, saveNote, switchStock } from "./note-write-api-fixtures.mjs";

test("two tabs preserve a stale draft until explicit latest-version adoption and manual save", async ({ page, context }) => {
  const api = noteApi(), second = await context.newPage();
  const original = api.notes.get(7).revision;
  await openNotes(page, api);
  await openNotes(second, api);
  await editNote(second, 7, "第二页尚未保存的研究判断");
  await editNote(page, 7, "第一页已确认的研究判断");
  await saveNote(page, 7);
  await expect(noteForm(page, 7)).toBeHidden();
  const latest = api.notes.get(7).revision;
  expect(latest).not.toBe(original);
  await saveNote(second, 7);
  const form = noteForm(second, 7);
  await expect(form.locator("[data-note-conflict]")).toContainText("草稿已保留");
  await expect(form.locator('[name="content"]')).toHaveValue("第二页尚未保存的研究判断");
  await expect(form).toHaveAttribute("data-note-revision", original);
  expect(api.writes).toHaveLength(2);
  expect(api.writes[1]).toMatchObject({ expected: original, status: 409 });
  await form.locator("[data-note-latest]").click();
  await expect(form.locator("[data-note-conflict]")).toContainText("第一页已确认的研究判断");
  expect(api.reads).toContain("/api/stock/notes/7");
  await expect(form).toHaveAttribute("data-note-revision", original);
  await form.locator('[data-note-rebase="keep"]').click();
  await expect(form).toHaveAttribute("data-note-revision", latest);
  await expect(form.locator('[name="content"]')).toHaveValue("第二页尚未保存的研究判断");
  expect(api.writes).toHaveLength(2);
  await saveNote(second, 7);
  await expect(form).toBeHidden();
  expect(api.writes[2]).toMatchObject({ expected: latest, status: 200 });
  expect(api.notes.get(7).content).toBe("第二页尚未保存的研究判断");
});

test("stale hide and delete carry their observed revision and cannot change another tab's note", async ({ page, context }) => {
  const api = noteApi(), second = await context.newPage();
  const original = api.notes.get(7).revision;
  await openNotes(page, api);
  await openNotes(second, api);
  await editNote(page, 7, "已由另一窗口更新");
  await saveNote(page, 7);
  await expect(noteForm(page, 7)).toBeHidden();
  const latest = { ...api.notes.get(7) };
  await second.locator('[data-note-toggle="7"]').click();
  await expect(second.locator('[data-note-row="7"] .row-action-feedback')).toContainText("版本冲突");
  await second.locator('[data-note-remove="7"]').click();
  await expect.poll(() => api.writes.length).toBe(3);
  await expect.poll(() => api.writes.at(-1)?.status).toBe(409);
  expect(api.writes.slice(1)).toMatchObject([
    { method: "PATCH", expected: original, body: { visible: false, expected_revision: original }, status: 409 },
    { method: "DELETE", expected: original, status: 409 },
  ]);
  expect(api.notes.get(7)).toEqual(latest);
  await expect(second.locator('[data-note-edit="7"]')).toBeVisible();
  await expect(noteForm(second, 7).locator("[data-note-conflict]")).toBeVisible();
});

for (const nextId of [7, 8]) {
  test(`pending save preserves newer note ${nextId} draft and confirmed commit when list readback fails`, async ({ page }) => {
    const api = noteApi();
    await openNotes(page, api);
    const original = api.notes.get(nextId).revision, release = api.pauseNextWrite();
    await editNote(page, 7, "本次已提交的内容");
    await saveNote(page, 7);
    await expect.poll(() => api.writes.length).toBe(1);
    if (nextId !== 7) await page.locator(`[data-note-edit="${nextId}"]`).click();
    const form = noteForm(page, nextId);
    await form.locator('[name="content"]').fill(`笔记${nextId}后续未提交草稿`);
    await form.locator('[name="price"]').fill("");
    await form.locator('[name="trade_date"]').fill("");
    api.failNotes = true;
    release();
    await expect(page.locator("#noteList")).toContainText("笔记已修改");
    await expect(page.locator("#noteList")).toContainText("列表刷新失败");
    await expect(page.locator('[data-note-row="7"] .editable-row-summary')).toContainText("本次已提交的内容");
    await expect(form).toBeVisible();
    await expect(form.locator('[name="content"]')).toHaveValue(`笔记${nextId}后续未提交草稿`);
    await expect(form.locator('[name="price"]')).toHaveValue("");
    await expect(form.locator('[name="trade_date"]')).toHaveValue("");
    const expected = nextId === 7 ? api.notes.get(7).revision : original;
    await expect(form).toHaveAttribute("data-note-revision", expected);
    await expect(form.locator('button[type="submit"]')).toBeEnabled();
    expect(api.writes).toHaveLength(1);
    api.failNotes = false;
    await saveNote(page, nextId);
    await expect(form).toBeHidden();
    expect(api.writes[1]).toMatchObject({ id: nextId, expected, status: 200,
      body: { content: `笔记${nextId}后续未提交草稿`, price: null, trade_date: null } });
  });
}

test("a late save after stock navigation commits only its original note and cannot replace the new stock draft", async ({ page }) => {
  const api = noteApi();
  await openNotes(page, api);
  const release = api.pauseNextWrite(), bankRevision = api.notes.get(9).revision;
  await editNote(page, 7, "茅台请求仍需完成保存");
  await saveNote(page, 7);
  await expect.poll(() => api.writes.length).toBe(1);
  await switchStock(page, "000001", "平安银行");
  await expect(page.locator('[data-note-edit="9"]')).toBeVisible();
  await editNote(page, 9, "银行的新草稿不能被茅台回执替换");
  const response = page.waitForResponse(reply => reply.url().endsWith("/api/stock/notes/7") && reply.request().method() === "PATCH");
  release();
  await response;
  await expect.poll(() => api.notes.get(7).content).toBe("茅台请求仍需完成保存");
  const form = noteForm(page, 9);
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect(form.locator('[name="content"]')).toHaveValue("银行的新草稿不能被茅台回执替换");
  await expect(form).toHaveAttribute("data-note-revision", bankRevision);
  await expect(page.locator("#noteList")).not.toContainText("茅台请求仍需完成保存");
  expect(api.writes).toHaveLength(1);
  await saveNote(page, 9);
  await expect(form).toBeHidden();
  expect(api.writes[1]).toMatchObject({ id: 9, expected: bankRevision, status: 200 });
});
