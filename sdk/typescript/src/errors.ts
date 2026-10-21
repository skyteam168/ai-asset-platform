/** Consumer SDK error from data-plane RFC 9457 Problem Details. */

export class SdkError extends Error {
  readonly code: string;
  readonly detail: string;
  readonly status: number;
  readonly body: Record<string, unknown>;

  constructor(
    code: string,
    detail = "",
    opts: { status?: number; body?: Record<string, unknown> } = {},
  ) {
    super(detail ? `${code}: ${detail}` : code);
    this.name = "SdkError";
    this.code = code;
    this.detail = detail;
    this.status = opts.status ?? 0;
    this.body = opts.body ?? {};
  }
}
