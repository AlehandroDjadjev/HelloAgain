enum PipelinePhase {
  idle,
  creatingSession,
  parsingIntent,
  buildingPlan,
  approvingPlan,
  executing,
  awaitingConfirmation,
  completed,
  failed,
  cancelled,
}

extension PipelinePhaseLabel on PipelinePhase {
  String get label => switch (this) {
        PipelinePhase.idle => 'Idle',
        PipelinePhase.creatingSession => 'Creating session…',
        PipelinePhase.parsingIntent => 'Parsing intent…',
        PipelinePhase.buildingPlan => 'Building plan…',
        PipelinePhase.approvingPlan => 'Approving plan…',
        PipelinePhase.executing => 'Executing…',
        PipelinePhase.awaitingConfirmation => 'Awaiting confirmation',
        PipelinePhase.completed => 'Completed',
        PipelinePhase.failed => 'Failed',
        PipelinePhase.cancelled => 'Cancelled',
      };

  bool get isTerminal => this == PipelinePhase.completed ||
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
  StepStatus status;

  StepEntry({
    required this.id,
    required this.type,
    required this.label,
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
