enum PipelinePhase {
  idle,
  creatingSession,
  parsingIntent,
  executing,
  awaitingConfirmation,
  awaitingUserInput,
  completed,
  failed,
  cancelled,
}

extension PipelinePhaseLabel on PipelinePhase {
  String get label => switch (this) {
    PipelinePhase.idle => 'Idle',
    PipelinePhase.creatingSession => 'Creating session...',
    PipelinePhase.parsingIntent => 'Parsing intent...',
    PipelinePhase.executing => 'Executing...',
    PipelinePhase.awaitingConfirmation => 'Awaiting confirmation',
    PipelinePhase.awaitingUserInput => 'Awaiting clarification',
    PipelinePhase.completed => 'Completed',
    PipelinePhase.failed => 'Failed',
    PipelinePhase.cancelled => 'Cancelled',
  };

  bool get isTerminal =>
      this == PipelinePhase.completed ||
      this == PipelinePhase.failed ||
      this == PipelinePhase.cancelled;

  bool get isRunning => !isTerminal && this != PipelinePhase.idle;
}

enum LogLevel { info, success, warning, error }

class LogEntry {
  final DateTime time;
  final String message;
  final LogLevel level;

  LogEntry(this.message, {this.level = LogLevel.info}) : time = DateTime.now();

  String get timeLabel {
    final t = time;
    return '${t.hour.toString().padLeft(2, '0')}:'
        '${t.minute.toString().padLeft(2, '0')}:'
        '${t.second.toString().padLeft(2, '0')}';
  }
}

class StepEntry {
  final String id;
  final String type;
  final String label;
  final String reasoning;
  StepStatus status;

  StepEntry({
    required this.id,
    required this.type,
    required this.label,
    this.reasoning = '',
    this.status = StepStatus.pending,
  });
}

enum StepStatus { pending, running, success, failed, skipped }

class ConfirmationRequest {
  final String confirmationId;
  final String stepId;
  final String appName;
  final String actionSummary;
  final String recipient;
  final String contentPreview;

  const ConfirmationRequest({
    required this.confirmationId,
    required this.stepId,
    required this.appName,
    required this.actionSummary,
    this.recipient = '',
    this.contentPreview = '',
  });

  factory ConfirmationRequest.fromJson(Map<String, dynamic> j) =>
      ConfirmationRequest(
        confirmationId: j['id'] as String,
        stepId: j['step_id'] as String? ?? '',
        appName: j['app_name'] as String? ?? '',
        actionSummary: j['action_summary'] as String? ?? '',
        recipient: j['recipient'] as String? ?? '',
        contentPreview: j['content_preview'] as String? ?? '',
      );
}

class UserInputRequest {
  final String queryId;
  final String question;
  final List<String> requiredFields;
  final List<String> candidates;
  final int attempt;
  final int maxAttempts;
  final String reason;
  final String whyUnresolved;

  const UserInputRequest({
    required this.queryId,
    required this.question,
    this.requiredFields = const [],
    this.candidates = const [],
    this.attempt = 1,
    this.maxAttempts = 3,
    this.reason = '',
    this.whyUnresolved = '',
  });

  factory UserInputRequest.fromJson(Map<String, dynamic> json) {
    final params = (json['params'] as Map?)?.cast<String, dynamic>() ?? json;
    List<String> readStringList(Object? value) {
      if (value is List) {
        return value.map((item) => item.toString()).toList();
      }
      return const [];
    }

    return UserInputRequest(
      queryId:
          (params['query_id'] ?? json['query_id'] ?? json['id'] ?? '')
              .toString(),
      question:
          (params['question'] ?? json['question'] ?? '').toString(),
      requiredFields: readStringList(
        params['required_fields'] ?? json['required_fields'],
      ),
      candidates: readStringList(params['candidates'] ?? json['candidates']),
      attempt:
          (params['attempt'] as num?)?.toInt() ??
          (json['attempt'] as num?)?.toInt() ??
          1,
      maxAttempts:
          (params['max_attempts'] as num?)?.toInt() ??
          (json['max_attempts'] as num?)?.toInt() ??
          3,
      reason: (params['reason'] ?? json['reason'] ?? '').toString(),
      whyUnresolved:
          (params['why_unresolved'] ?? json['why_unresolved'] ?? '').toString(),
    );
  }
}
