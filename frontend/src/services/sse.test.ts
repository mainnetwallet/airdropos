import { describe, expect, it } from 'vitest'
import { takeSseFrames } from './sse'

describe('takeSseFrames', () => {
  it('returns complete frames and keeps the unfinished tail', () => {
    const { frames, rest } = takeSseFrames(
      'event: reply_delta\ndata: {"a":1}\n\nevent: done\ndata: {"b"',
    )
    expect(frames).toEqual([{ event: 'reply_delta', data: '{"a":1}' }])
    expect(rest).toBe('event: done\ndata: {"b"')
  })

  it('completes a frame that arrives in two chunks', () => {
    const first = takeSseFrames('event: done\ndata: {"x":')
    expect(first.frames).toEqual([])
    const second = takeSseFrames(first.rest + '2}\n\n')
    expect(second.frames).toEqual([{ event: 'done', data: '{"x":2}' }])
    expect(second.rest).toBe('')
  })

  it('ignores keep-alive comments', () => {
    const { frames } = takeSseFrames(': keep-alive\n\nevent: a\ndata: 1\n\n')
    expect(frames).toEqual([{ event: 'a', data: '1' }])
  })

  it('handles CRLF even when it is split between chunks', () => {
    const first = takeSseFrames('event: a\r\ndata: 1\r\n\r')
    expect(first.frames).toEqual([])
    const second = takeSseFrames(first.rest + '\n')
    expect(second.frames).toEqual([{ event: 'a', data: '1' }])
  })

  it('joins multi-line data', () => {
    const { frames } = takeSseFrames('data: one\ndata: two\n\n')
    expect(frames).toEqual([{ event: 'message', data: 'one\ntwo' }])
  })
})
