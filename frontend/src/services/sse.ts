/**
 * Minimal Server-Sent Events framing for a `fetch` body.
 *
 * `EventSource` cannot be used for the agent chat: it only issues GET requests and cannot
 * send the Authorization header. The body is read as text instead and cut into frames here.
 * A frame ends at a blank line; `: comment` lines (the server's keep-alives) are ignored.
 */

export interface SseFrame {
  event: string
  data: string
}

/**
 * Split everything received so far into complete frames plus the unfinished remainder.
 * Pass the remainder back in, with the next chunk appended, on the following call.
 */
export function takeSseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  // A "\r\n" can be split across two chunks, so line endings are normalised on the
  // whole accumulated buffer, not per chunk.
  const parts = buffer.replace(/\r\n/g, '\n').split('\n\n')
  const rest = parts.pop() ?? ''
  const frames: SseFrame[] = []

  for (const part of parts) {
    let event = 'message'
    const data: string[] = []
    for (const line of part.split('\n')) {
      if (line === '' || line.startsWith(':')) continue
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
    }
    if (data.length > 0) frames.push({ event, data: data.join('\n') })
  }
  return { frames, rest }
}
