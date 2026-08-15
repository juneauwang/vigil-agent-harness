export function logError(error: unknown): void {
  if (!process.env.VIGIL_INK_DEBUG_ERRORS) {
    return
  }

  console.error(error)
}
