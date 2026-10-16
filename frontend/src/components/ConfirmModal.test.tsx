import { afterEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import ConfirmModal from './ConfirmModal'
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

async function open() {
  const onConfirm = vi.fn()
  const onClose = vi.fn()
  const host = document.createElement('div')
  document.body.appendChild(host)
  await act(async () => {
    createRoot(host).render(
      <ConfirmModal title="Delete conversation" onConfirm={onConfirm} onClose={onClose}>
        Sure?
      </ConfirmModal>,
    )
  })
  const button = (label: string) =>
    Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.trim() === label) as HTMLButtonElement
  return { onConfirm, onClose, button }
}

describe('ConfirmModal', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('focuses Cancel first and does not confirm on its own', async () => {
    const { onConfirm, button } = await open()
    expect(document.activeElement).toBe(button('Cancel'))
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('confirms only when the confirm button is pressed', async () => {
    const { onConfirm, onClose, button } = await open()
    await act(async () => button('Delete').click())
    expect(onConfirm).toHaveBeenCalledTimes(1)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('Cancel closes without confirming', async () => {
    const { onConfirm, onClose, button } = await open()
    await act(async () => button('Cancel').click())
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })
})
