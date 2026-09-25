/**
 * REST 客户端封装（03 §5.1）：统一解包 `{ok,data,meta}`，错误抛 `ApiError{code,message}`；
 * token 自 URL `?token=` 注入（03 §8.3）。
 */
import { errorEnvelopeSchema } from '../proto';
import { envelopeMetaSchema } from '../proto';
import type { z } from 'zod';

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export interface ApiResult<T> {
  data: T;
  meta: { watermark_tick: number } & Record<string, unknown>;
}

export function getToken(): string {
  return new URLSearchParams(window.location.search).get('token') ?? '';
}

export function withToken(url: string): string {
  const token = getToken();
  if (!token) return url;
  return url + (url.includes('?') ? '&' : '?') + `token=${encodeURIComponent(token)}`;
}

export async function apiGet<T>(url: string, schema?: z.ZodType<T>): Promise<ApiResult<T>> {
  const res = await fetch(withToken(url));
  const body = await res.json();
  if (!res.ok || body?.ok === false) {
    const err = errorEnvelopeSchema.safeParse(body);
    if (err.success) throw new ApiError(err.data.error.code, err.data.error.message, res.status);
    throw new ApiError('http_error', `HTTP ${res.status}`, res.status);
  }
  const meta = envelopeMetaSchema.parse(body.meta);
  const data = schema ? schema.parse(body.data) : (body.data as T);
  return { data, meta };
}
