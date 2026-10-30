// Package buffer provides a zero-allocation, reusable 512 KiB byte buffer pool
// backed by sync.Pool to eliminate heap ballooning and GC thrashing during
// concurrent multi-stream downloads on a 2 vCPU, 4 GB RAM VPS.
package buffer

import (
	"sync"
	"sync/atomic"
)

const (
	// ChunkSize512KB is the mandatory fixed 512 KiB (524,288 bytes) stream buffer size.
	ChunkSize512KB = 512 * 1024
)

// Pool manages a thread-safe sync.Pool of fixed 512 KiB byte slices.
type Pool struct {
	pool     sync.Pool
	borrowed atomic.Int64
	allocs   atomic.Uint64
}

// DefaultPool is the package-level shared 512 KiB buffer pool instance.
var DefaultPool = NewPool()

// NewPool constructs a new 512 KiB reusable buffer pool.
func NewPool() *Pool {
	p := &Pool{}
	p.pool.New = func() any {
		p.allocs.Add(1)
		buf := make([]byte, ChunkSize512KB)
		return &buf
	}
	return p
}

// Get borrows a 512 KiB byte slice pointer from the pool.
// Callers MUST return the buffer via Put(buf) when finished (typically via defer).
func (p *Pool) Get() *[]byte {
	p.borrowed.Add(1)
	bufPtr := p.pool.Get().(*[]byte)
	if cap(*bufPtr) < ChunkSize512KB {
		fresh := make([]byte, ChunkSize512KB)
		bufPtr = &fresh
	} else {
		*bufPtr = (*bufPtr)[:ChunkSize512KB]
	}
	return bufPtr
}

// Put returns a borrowed 512 KiB buffer back to the sync.Pool for immediate reuse.
func (p *Pool) Put(bufPtr *[]byte) {
	if bufPtr == nil || cap(*bufPtr) < ChunkSize512KB {
		return
	}
	*bufPtr = (*bufPtr)[:ChunkSize512KB]
	p.borrowed.Add(-1)
	p.pool.Put(bufPtr)
}

// ActiveBorrowed returns the instantaneous count of 512 KiB buffers currently checked out.
func (p *Pool) ActiveBorrowed() int64 {
	return p.borrowed.Load()
}

// TotalAllocations returns the cumulative number of underlying 512 KiB slices allocated.
func (p *Pool) TotalAllocations() uint64 {
	return p.allocs.Load()
}
