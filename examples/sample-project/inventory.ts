// Entirely fictional source used to demonstrate evidence boundaries.
export function formatInventory(count: number | null | undefined): string {
  if (count === null || count === undefined) {
    return "Loading";
  }
  if (!count) {
    return "";
  }
  return `${count} items`;
}
