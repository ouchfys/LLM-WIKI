// A late response must not overwrite a newer navigation or deletion.
export function createRequestGuard() {
  let revision = 0
  return {
    begin: () => ++revision,
    invalidate: () => { ++revision },
    isCurrent: (ticket: number) => ticket === revision,
  }
}
