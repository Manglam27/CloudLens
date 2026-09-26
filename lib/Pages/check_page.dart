import 'dart:async';

import 'package:amplify_flutter/amplify_flutter.dart';
import 'package:cloud_lens/check_service.dart';
import 'package:flutter/material.dart';

/// Colour and label for each verdict, shared with the History screen.
class Verdict {
  static Color color(String? verdict) => switch (verdict) {
        'TRUE' => Colors.green.shade700,
        'FALSE' => Colors.red.shade700,
        _ => Colors.grey.shade600,
      };

  static String label(String? verdict) => switch (verdict) {
        'TRUE' => 'True',
        'FALSE' => 'False',
        _ => 'Unverified',
      };

  static Widget pill(String? verdict, {double fontSize = 13}) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        decoration: BoxDecoration(color: color(verdict), borderRadius: BorderRadius.circular(20)),
        child: Text(label(verdict),
            style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold, fontSize: fontSize)),
      );
}

/// Runs a credibility check and shows its progress, then its result.
///
/// Pass [imageKey] to start a new check on a cloud photo (FR 1.4), or
/// [checkId] to open an earlier check from History without re-running it (FR 1.6).
class CheckPage extends StatefulWidget {
  final String? imageKey;
  final String? checkId;

  const CheckPage({super.key, this.imageKey, this.checkId})
      : assert(imageKey != null || checkId != null);

  @override
  State<CheckPage> createState() => _CheckPageState();
}

class _CheckPageState extends State<CheckPage> {
  static const _pollInterval = Duration(seconds: 2);
  static const _giveUpAfter = Duration(seconds: 90);
  static const _stages = [
    ('READING_TEXT', 'Reading text'),
    ('FINDING_CLAIM', 'Finding the claim'),
    ('SEARCHING_SOURCES', 'Searching sources'),
    ('SCORING', 'Scoring'),
  ];

  CheckResult? _check;
  String? _error;
  String? _imageUrl;
  Timer? _timer;
  DateTime? _startedPolling;

  @override
  void initState() {
    super.initState();
    _begin();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _begin() async {
    setState(() {
      _error = null;
      _check = null;
    });
    try {
      final check = widget.checkId != null
          ? await CheckService.get(widget.checkId!)
          : await CheckService.start(widget.imageKey!);
      if (!mounted) return;
      setState(() => _check = check);
      _loadImage(check.imageKey ?? widget.imageKey);
      if (!check.isFinished) _startPolling();
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  Future<void> _loadImage(String? key) async {
    if (key == null || _imageUrl != null) return;
    try {
      final result = await Amplify.Storage.getUrl(path: StoragePath.fromString(key)).result;
      if (mounted) setState(() => _imageUrl = result.url.toString());
    } catch (_) {
      // The photo may have been deleted; the result is still shown.
    }
  }

  void _startPolling() {
    _startedPolling = DateTime.now();
    _timer?.cancel();
    _timer = Timer.periodic(_pollInterval, (_) => _poll());
  }

  Future<void> _poll() async {
    final check = _check;
    if (check == null) return;
    if (DateTime.now().difference(_startedPolling!) > _giveUpAfter) {
      _timer?.cancel();
      setState(() => _error = 'The check is taking longer than expected. Open it again from History later.');
      return;
    }
    try {
      final latest = await CheckService.get(check.checkId);
      if (!mounted) return;
      setState(() => _check = latest);
      if (latest.isFinished) _timer?.cancel();
    } catch (_) {
      // A single failed poll is retried on the next tick.
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Credibility check'),
        backgroundColor: const Color.fromARGB(255, 84, 152, 247),
        foregroundColor: Colors.white,
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          if (_imageUrl != null)
            ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: Image.network(_imageUrl!, height: 200, fit: BoxFit.contain),
            ),
          const SizedBox(height: 20),
          _body(),
        ],
      ),
    );
  }

  Widget _body() {
    final check = _check;
    if (_error != null) return _problem(_error!);
    if (check == null) return const Center(child: CircularProgressIndicator());
    if (check.isFailed) return _problem(check.error ?? 'The check could not be completed.');
    if (check.isDone) return _result(check);
    return _progress(check);
  }

  Widget _progress(CheckResult check) {
    final current = _stages.indexWhere((s) => s.$1 == check.status);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (var i = 0; i < _stages.length; i++)
          ListTile(
            dense: true,
            leading: i < current
                ? Icon(Icons.check_circle, color: Colors.green.shade700)
                : i == current
                    ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.5))
                    : Icon(Icons.radio_button_unchecked, color: Colors.grey.shade400),
            title: Text(_stages[i].$2,
                style: TextStyle(fontWeight: i == current ? FontWeight.bold : FontWeight.normal)),
          ),
        const SizedBox(height: 8),
        LinearProgressIndicator(value: (current + 1).clamp(0, _stages.length) / (_stages.length + 1)),
        const SizedBox(height: 8),
        Text('Usually under 30 seconds.', style: TextStyle(color: Colors.grey.shade600)),
      ],
    );
  }

  Widget _result(CheckResult check) {
    final score = check.score;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Center(child: Verdict.pill(check.verdict, fontSize: 20)),
        const SizedBox(height: 16),
        Center(
          child: Text(score == null ? 'No score' : '$score / 100',
              style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
        ),
        if (score != null) ...[
          const SizedBox(height: 8),
          LinearProgressIndicator(
            value: score / 100,
            color: Verdict.color(check.verdict),
            backgroundColor: Colors.grey.shade300,
          ),
        ],
        const SizedBox(height: 24),
        if (check.claim != null) ...[
          const Text('Claim', style: TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 4),
          Text('“${check.claim}”', style: const TextStyle(fontStyle: FontStyle.italic)),
          const SizedBox(height: 16),
        ],
        const Text('Why', style: TextStyle(fontWeight: FontWeight.bold)),
        const SizedBox(height: 4),
        Text(check.reason ?? ''),
        if (check.cached) ...[
          const SizedBox(height: 16),
          Text('This screenshot was checked before, so the saved result is shown.',
              style: TextStyle(color: Colors.grey.shade600, fontSize: 12)),
        ],
      ],
    );
  }

  Widget _problem(String message) {
    return Column(
      children: [
        Icon(Icons.error_outline, color: Colors.red.shade700, size: 40),
        const SizedBox(height: 12),
        Text(message, textAlign: TextAlign.center),
        const SizedBox(height: 16),
        if (widget.imageKey != null)
          ElevatedButton(onPressed: _begin, child: const Text('Try again')),
      ],
    );
  }
}
