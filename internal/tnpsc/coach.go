package tnpsc

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand"
	"time"

	"github.com/starfall-org/starfall-go/internal/database"
)

var (
	// TargetExamDate is the scheduled date for TNPSC Group 4 examination.
	TargetExamDate = time.Date(2026, time.December, 20, 10, 0, 0, 0, time.UTC)
)

// SyllabusModule represents a study module in the TNPSC syllabus.
type SyllabusModule struct {
	Subject string   `json:"subject"`
	Topics  []string `json:"topics"`
	Weight  int      `json:"weight"` // Relative percentage weight
}

// DailyStudyPlan packages a daily study syllabus and practice questions.
type DailyStudyPlan struct {
	Date          string               `json:"date"`
	DaysRemaining int                  `json:"days_remaining"`
	FocusSubject  string               `json:"focus_subject"`
	Topic         string               `json:"topic"`
	KeyConcepts   []string             `json:"key_concepts"`
	PYQQuestions  []database.PYQRecord `json:"pyq_questions"`
}

// CoachService manages TNPSC daily study schedules, countdown, and FTS5 search.
type CoachService struct {
	db      *database.DBManager
	logger  *slog.Logger
	modules []SyllabusModule
}

// NewCoachService initializes the TNPSC AI Study Coach.
func NewCoachService(db *database.DBManager, logger *slog.Logger) *CoachService {
	if logger == nil {
		logger = slog.Default()
	}
	return &CoachService{
		db:     db,
		logger: logger,
		modules: []SyllabusModule{
			{
				Subject: "Tamil Heritage & Literature",
				Topics: []string{
					"Thirukkural - Arathupal and Porutpal",
					"Sangam Literature - Ettuthogai & Pathupattu",
					"Silapathikaram & Manimekalai",
					"Bharathiyar and Bharathidasan Modern Poetry",
					"Tamil Renaissance & Dravidian Movement",
				},
				Weight: 35,
			},
			{
				Subject: "Indian Constitution & Polity",
				Topics: []string{
					"Preamble & Fundamental Rights (Articles 12-35)",
					"Directive Principles of State Policy & Fundamental Duties",
					"Union Executive: President, Prime Minister, Parliament",
					"State Government: Governor, Chief Minister, Assembly",
					"Judiciary: Supreme Court, High Court & Panchayati Raj",
				},
				Weight: 25,
			},
			{
				Subject: "General Science & Geography",
				Topics: []string{
					"Tamil Nadu Physical Geography and Rivers",
					"Climate, Monsoon and Natural Resources of India",
					"Human Physiology, Diseases and Nutrition",
					"Environmental Science & Ecology",
				},
				Weight: 20,
			},
			{
				Subject: "Aptitude & Mental Ability",
				Topics: []string{
					"Simplification, Percentage, Profit & Loss",
					"Ratio, Proportion, Time & Work",
					"Simple & Compound Interest",
					"Area, Volume and Logical Reasoning",
				},
				Weight: 20,
			},
		},
	}
}

// DaysRemaining calculates the number of calendar days left until the 2026-12-20 exam.
func (c *CoachService) DaysRemaining(now time.Time) int {
	diff := TargetExamDate.Sub(now)
	days := int(diff.Hours() / 24)
	if days < 0 {
		return 0
	}
	return days
}

// GenerateDailyPlan generates the daily study assignment for a given date.
func (c *CoachService) GenerateDailyPlan(ctx context.Context, now time.Time) (*DailyStudyPlan, error) {
	daysLeft := c.DaysRemaining(now)

	// Deterministic selection based on day of year
	dayOfYear := now.YearDay()
	modIdx := dayOfYear % len(c.modules)
	mod := c.modules[modIdx]

	topicIdx := (dayOfYear / len(c.modules)) % len(mod.Topics)
	topic := mod.Topics[topicIdx]

	var questions []database.PYQRecord
	if c.db != nil {
		q, err := c.db.SearchPYQ(ctx, topic, 3)
		if err == nil && len(q) > 0 {
			questions = q
		}
	}

	return &DailyStudyPlan{
		Date:          now.Format("2006-01-02"),
		DaysRemaining: daysLeft,
		FocusSubject:  mod.Subject,
		Topic:         topic,
		KeyConcepts: []string{
			"Memorize standard terminology and historical years",
			"Solve minimum 10 previous year questions on this topic",
			"Review TNPSC 6th to 10th standard Samacheer Kalvi textbooks",
		},
		PYQQuestions: questions,
	}, nil
}

// Next0800IST calculates the next occurrence of 08:00 AM Indian Standard Time (UTC+05:30).
func Next0800IST(now time.Time) time.Time {
	istLoc := time.FixedZone("IST", 5*3600+1800)
	nowIST := now.In(istLoc)

	target := time.Date(nowIST.Year(), nowIST.Month(), nowIST.Day(), 8, 0, 0, 0, istLoc)
	if !nowIST.Before(target) {
		target = target.AddDate(0, 0, 1)
	}
	return target
}

// SeedSamplePYQ imports foundational Tamil and Polity questions into SQLite FTS5 index.
func (c *CoachService) SeedSamplePYQ(ctx context.Context) error {
	if c.db == nil {
		return nil
	}

	samples := []database.PYQRecord{
		{
			QuestionID:  "pyq_tam_01",
			Year:        "2022",
			ExamName:    "Group 4",
			Subject:     "Tamil Heritage",
			Question:    "அகர முதல எழுத்தெல்லாம் ஆதி பகவன் முதற்றே உலகு - இக்குறளில் பயின்று வந்துள்ள அணி யாது?",
			OptionsJSON: []string{"எடுத்துக்காட்டு உவமையணி", "உவமையணி", "சொற்பொருள் பின்வருநிலையணி", "பிறிது மொழிதலணி"},
			Answer:      "எடுத்துக்காட்டு உவமையணி",
			Explanation: "உவமை ஒரு தொடராகவும் உவமேயம் ஒரு தொடராகவும் உவம உருபு மறைந்து வந்தால் எடுத்துக்காட்டு உவமையணி ஆகும்.",
		},
		{
			QuestionID:  "pyq_pol_01",
			Year:        "2023",
			ExamName:    "Group 4",
			Subject:     "Indian Constitution",
			Question:    "இந்திய அரசியலமைப்பின் 21A பிரிவு எதனைப் பற்றிக் கூறுகிறது?",
			OptionsJSON: []string{"தொடக்கக் கல்வி கற்கும் உரிமை", "சுரண்டலுக்கு எதிரான உரிமை", "பேச்சுரிமை", "சமத்துவ உரிமை"},
			Answer:      "தொடக்கக் கல்வி கற்கும் உரிமை",
			Explanation: "86வது அரசியலமைப்பு திருத்தச் சட்டம் 2002 மூலம் 6 முதல் 14 வயது வரையிலான குழந்தைகளுக்கு இலவச கட்டாயக் கல்வி உரிமை வழங்கப்பட்டது.",
		},
	}

	for _, s := range samples {
		item := s
		if err := c.db.IndexPYQ(ctx, &item); err != nil {
			c.logger.Debug("seeding sample pyq", "error", err)
		}
	}
	return nil
}
