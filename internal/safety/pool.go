package safety

import (
	"context"
	"errors"
	"sync"
)

var (
	ErrPoolClosed    = errors.New("safety: worker pool closed")
	ErrQueueOverflow = errors.New("safety: bounded job queue full")
)

var (
	// CopyBufPool supplies reusable 64 KiB slices for io.CopyBuffer across HTTP/disk streams.
	CopyBufPool = sync.Pool{
		New: func() any {
			b := make([]byte, 64*1024)
			return &b
		},
	}
	// HashBufPool supplies reusable 256 KiB slices for .torrent SHA-1 piece hashing.
	HashBufPool = sync.Pool{
		New: func() any {
			b := make([]byte, 256*1024)
			return &b
		},
	}
)

// GetCopyBuf acquires a 64 KiB buffer from CopyBufPool.
func GetCopyBuf() *[]byte {
	return CopyBufPool.Get().(*[]byte)
}

// PutCopyBuf returns a 64 KiB buffer back to CopyBufPool.
func PutCopyBuf(b *[]byte) {
	if b != nil && len(*b) == 64*1024 {
		CopyBufPool.Put(b)
	}
}

// GetHashBuf acquires a 256 KiB buffer from HashBufPool.
func GetHashBuf() *[]byte {
	return HashBufPool.Get().(*[]byte)
}

// PutHashBuf returns a 256 KiB buffer back to HashBufPool.
func PutHashBuf(b *[]byte) {
	if b != nil && len(*b) == 256*1024 {
		HashBufPool.Put(b)
	}
}

// JobFunc is a unit of work executable by BoundedPool.
type JobFunc func(ctx context.Context) error

// BoundedPool limits concurrently executing goroutines and queues pending work
// up to a bounded size to prevent unbounded memory growth during heavy crawls.
type BoundedPool struct {
	queue   chan JobFunc
	wg      sync.WaitGroup
	closeMu sync.RWMutex
	closed  bool
}

// NewBoundedPool initializes a worker pool with a fixed number of workers
// and a bounded channel capacity.
func NewBoundedPool(ctx context.Context, workers, queueSize int) *BoundedPool {
	if workers <= 0 {
		workers = 1
	}
	if queueSize <= 0 {
		queueSize = workers * 2
	}
	p := &BoundedPool{
		queue: make(chan JobFunc, queueSize),
	}
	for i := 0; i < workers; i++ {
		p.wg.Add(1)
		go func() {
			defer p.wg.Done()
			for {
				select {
				case <-ctx.Done():
					return
				case job, ok := <-p.queue:
					if !ok {
						return
					}
					_ = job(ctx)
				}
			}
		}()
	}
	return p
}

// TrySubmit attempts to enqueue a job without blocking. Returns ErrQueueOverflow if full.
func (p *BoundedPool) TrySubmit(job JobFunc) error {
	p.closeMu.RLock()
	defer p.closeMu.RUnlock()
	if p.closed {
		return ErrPoolClosed
	}
	select {
	case p.queue <- job:
		return nil
	default:
		return ErrQueueOverflow
	}
}

// Submit enqueues a job, blocking until space is available or context is cancelled.
func (p *BoundedPool) Submit(ctx context.Context, job JobFunc) error {
	p.closeMu.RLock()
	defer p.closeMu.RUnlock()
	if p.closed {
		return ErrPoolClosed
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case p.queue <- job:
		return nil
	}
}

// Close gracefully marks the pool closed and stops accepting new submissions.
func (p *BoundedPool) Close() {
	p.closeMu.Lock()
	if !p.closed {
		p.closed = true
		close(p.queue)
	}
	p.closeMu.Unlock()
	p.wg.Wait()
}

// BoundedRingBuffer is a fixed-capacity thread-safe circular buffer implementing io.Writer.
// It retains at most the last capacity bytes written, discarding older bytes without heap reallocation.
type BoundedRingBuffer struct {
	mu   sync.Mutex
	buf  []byte
	size int
	head int
	full bool
}

// NewBoundedRingBuffer creates a BoundedRingBuffer with fixed byte capacity.
func NewBoundedRingBuffer(capacity int) *BoundedRingBuffer {
	if capacity <= 0 {
		capacity = 64 * 1024
	}
	return &BoundedRingBuffer{
		buf:  make([]byte, capacity),
		size: capacity,
	}
}

// Write writes bytes into the ring buffer, overwriting the oldest entries if capacity is reached.
func (r *BoundedRingBuffer) Write(p []byte) (n int, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	n = len(p)
	if n == 0 {
		return 0, nil
	}
	if n >= r.size {
		// p is larger than buffer capacity; copy only the tail
		copy(r.buf, p[n-r.size:])
		r.head = 0
		r.full = true
		return n, nil
	}
	for _, b := range p {
		r.buf[r.head] = b
		r.head = (r.head + 1) % r.size
		if r.head == 0 {
			r.full = true
		}
	}
	return n, nil
}

// Bytes returns a slice containing the current buffer content in write order.
func (r *BoundedRingBuffer) Bytes() []byte {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.full {
		out := make([]byte, r.head)
		copy(out, r.buf[:r.head])
		return out
	}
	out := make([]byte, r.size)
	copy(out, r.buf[r.head:])
	copy(out[r.size-r.head:], r.buf[:r.head])
	return out
}

// String returns the buffer content as a string.
func (r *BoundedRingBuffer) String() string {
	return string(r.Bytes())
}

// Reset clears the buffer.
func (r *BoundedRingBuffer) Reset() {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.head = 0
	r.full = false
}
