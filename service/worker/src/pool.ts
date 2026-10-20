/** Run `fn` over `items` with at most `limit` calls in flight. */
export async function mapPool<T>(
  items: readonly T[],
  limit: number,
  fn: (item: T, index: number) => Promise<void>,
): Promise<void> {
  if (!items.length) return;
  const width = Math.max(1, Math.min(limit, items.length));
  let next = 0;
  const worker = async () => {
    for (;;) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      await fn(items[index], index);
    }
  };
  await Promise.all(Array.from({ length: width }, () => worker()));
}
