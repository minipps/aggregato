import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  creator: vi.fn(),
  mergeCreator: vi.fn(),
  splitCreator: vi.fn(),
  undoMerge: vi.fn(),
  work: vi.fn(),
  mergeWork: vi.fn(),
}))

vi.mock('@/api/client', () => ({
  creator: mocks.creator,
  mergeCreator: mocks.mergeCreator,
  splitCreator: mocks.splitCreator,
  undoMerge: mocks.undoMerge,
  work: mocks.work,
  mergeWork: mocks.mergeWork,
  toProblem: (error: unknown) => ({
    type: 'about:blank',
    title: 'Request failed',
    status: error instanceof Error && error.message.includes('409') ? 409 : 0,
    detail: error instanceof Error ? error.message : String(error),
  }),
}))

vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: 'item-1' } }) }))

import { readonlyAccess } from '@/api/session'
import Creator from '../Creator.vue'
import Work from '../Work.vue'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

async function settle(): Promise<void> {
  await Promise.resolve()
  await nextTick()
}

beforeEach(() => {
  readonlyAccess.value = false
  mocks.creator.mockResolvedValue({
    id: 'item-1',
    name: 'Creator',
    kind: 'person',
    credits_by_role: {
      writer: [{ id: 9, creator_id: 'item-1', creator_name: 'Creator', role: 'writer', position: 0, source: 'Book', link_confidence: 'matched' }],
    },
  })
  mocks.work.mockResolvedValue({
    id: 'item-1',
    title: 'Work',
    media_type: 'film',
    media_family: 'screen',
    entries: [],
    opinions: [],
    credits: [],
    external_ids: [],
    siblings: [],
  })
  mocks.mergeCreator.mockReset()
  mocks.splitCreator.mockReset()
  mocks.undoMerge.mockReset()
  mocks.mergeWork.mockReset()
})

afterEach(() => {
  readonlyAccess.value = undefined
  vi.clearAllMocks()
})

describe('identity mutation feedback', () => {
  it('shows a pending work merge and keeps its dialog open when the server refuses it', async () => {
    const operation = deferred<{ id: number }>()
    mocks.mergeWork.mockReturnValue(operation.promise)
    const wrapper = mount(Work, { global: { stubs: { RouterLink: true } } })
    await settle()

    await wrapper.get('button').trigger('click')
    await wrapper.get('input[aria-describedby="merge-help"]').setValue('duplicate-id')
    await wrapper.get('form').trigger('submit')
    await nextTick()

    expect(wrapper.text()).toContain('Merging…')
    expect(wrapper.findAll('button').find((button) => button.text() === 'Merging…')?.attributes('disabled')).toBeDefined()
    operation.reject(new Error('409 safe undo refused'))
    await settle()

    expect(wrapper.text()).toContain('409 safe undo refused')
    expect(wrapper.get('input[aria-describedby="merge-help"]').element).toBeDefined()
  })

  it('shows pending and error feedback for creator splits', async () => {
    const operation = deferred<{ id: number }>()
    mocks.splitCreator.mockReturnValue(operation.promise)
    const wrapper = mount(Creator)
    await settle()

    await wrapper.findAll('button').find((button) => button.text() === 'Split credits')!.trigger('click')
    await wrapper.get('input[type="checkbox"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await nextTick()

    expect(wrapper.text()).toContain('Splitting…')
    operation.reject(new Error('Split was refused'))
    await settle()

    expect(wrapper.text()).toContain('Split was refused')
    expect(wrapper.text()).toContain('Split 1 credit')
  })
})
