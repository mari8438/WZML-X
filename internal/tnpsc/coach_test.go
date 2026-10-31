package tnpsc

import (
	"context"
	"testing"
	"time"
)

func TestTNPSCCountdownAnd0800IST(t *testing.T) {
	coach := NewCoachService(nil, nil)

	// Fixed test date: 2026-10-31
	testNow := time.Date(2026, time.October, 31, 12, 0, 0, 0, time.UTC)
	daysLeft := coach.DaysRemaining(testNow)
	if daysLeft <= 0 || daysLeft > 60 {
		t.Fatalf("expected ~50 days remaining from 2026-10-31 to 2026-12-20, got %d", daysLeft)
	}

	// Test Next 08:00 IST calculation
	nextIST := Next0800IST(testNow)
	if nextIST.Hour() != 8 || nextIST.Minute() != 0 {
		t.Fatalf("expected next IST hour 8:00, got %02d:%02d", nextIST.Hour(), nextIST.Minute())
	}
	if !nextIST.After(testNow) {
		t.Fatalf("expected next IST to be in the future, got %v", nextIST)
	}

	// Test Daily Plan generation
	plan, err := coach.GenerateDailyPlan(context.Background(), testNow)
	if err != nil {
		t.Fatalf("failed to generate daily plan: %v", err)
	}
	if plan.FocusSubject == "" || plan.Topic == "" {
		t.Fatalf("empty focus subject or topic in plan: %+v", plan)
	}
	if len(plan.KeyConcepts) == 0 {
		t.Fatalf("expected key concepts in study plan, got 0")
	}
}
