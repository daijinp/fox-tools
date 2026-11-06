package main

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"io"
	"net"
	"time"

	"github.com/go-sql-driver/mysql"
	"golang.org/x/crypto/ssh"
)

type querySession struct {
	cfg         config
	configDir   string
	ssh         *ssh.Client
	db          *sql.DB
	planChecked bool
}

func (s *querySession) close() {
	// Closing the transport first unblocks any stuck SSH channel reads.
	if s.ssh != nil {
		s.ssh.Close()
		s.ssh = nil
	}
	if s.db != nil {
		s.db.Close()
		s.db = nil
	}
	s.planChecked = false
}

func (s *querySession) connect(ctx context.Context) error {
	if s.db != nil {
		return nil
	}
	client, err := connectSSH(ctx, s.cfg.SSH, s.configDir)
	if err != nil {
		return err
	}
	s.ssh = client
	stop := context.AfterFunc(ctx, func() { client.Close() })
	defer stop()
	s.db, err = connectMySQL(ctx, s.cfg, client)
	if err != nil {
		return err
	}
	fmt.Println("SSH 隧道和 MySQL 连接成功")
	return nil
}

// Retries encompass connection setup, preflight and full row iteration. A timed
// out operation closes SSH itself, because SSH channels do not support deadlines.
func (s *querySession) execute(ctx context.Context, batch []string, operation func(context.Context, queryer) error) error {
	return retryOperation(ctx, s.cfg.Query, func() error {
		attemptCtx, cancel := context.WithTimeout(ctx, time.Duration(s.cfg.Query.TimeoutSeconds)*time.Second)
		defer cancel()
		err := s.connect(attemptCtx)
		if err == nil {
			client := s.ssh
			stop := context.AfterFunc(attemptCtx, func() { client.Close() })
			if !s.planChecked {
				err = checkRequiredIndexes(s.db, s.cfg.Query.TimeoutSeconds)
				if err == nil {
					err = checkQueryPlan(s.db, batch, s.cfg.Query.TimeoutSeconds)
				}
				if err == nil {
					// Use the first actual batch after (re)connection for EXPLAIN.
					s.planChecked = true
				}
			}
			if err == nil {
				err = operation(attemptCtx, s.db)
			}
			stop()
		}
		if attemptCtx.Err() != nil {
			err = attemptCtx.Err()
		}
		if err != nil {
			s.close()
		}
		return err
	})
}

func retryOperation(ctx context.Context, cfg queryConfig, operation func() error) error {
	delay := min(time.Duration(cfg.RetryIntervalSeconds)*time.Second, 30*time.Second)
	var err error
	for attempt := 1; attempt <= cfg.MaxAttempts; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		err = operation()
		if err == nil {
			return nil
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if !isRetryable(err) || attempt == cfg.MaxAttempts {
			return fmt.Errorf("尝试 %d/%d 失败：%w", attempt, cfg.MaxAttempts, err)
		}
		fmt.Printf("临时连接/查询故障：%v；%s 后重建连接并重试（下一次 %d/%d）\n", err, delay, attempt+1, cfg.MaxAttempts)
		if err := waitContext(ctx, delay); err != nil {
			return err
		}
		delay = min(delay*2, 30*time.Second)
	}
	return err
}

func isRetryable(err error) bool {
	for _, candidate := range []error{context.DeadlineExceeded, driver.ErrBadConn, mysql.ErrInvalidConn, io.EOF, io.ErrUnexpectedEOF, net.ErrClosed} {
		if errors.Is(err, candidate) {
			return true
		}
	}
	var networkError net.Error
	if errors.As(err, &networkError) {
		return true
	}
	var mysqlError *mysql.MySQLError
	if errors.As(err, &mysqlError) {
		switch mysqlError.Number {
		case 1040, 1042, 1047, 1053, 1158, 1159, 1160, 1161, 1205, 1213, 2006, 2013, 4031:
			return true
		}
	}
	var channelError *ssh.OpenChannelError
	if errors.As(err, &channelError) {
		return channelError.Reason == ssh.ConnectionFailed || channelError.Reason == ssh.ResourceShortage
	}
	return false
}
