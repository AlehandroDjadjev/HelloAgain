from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .services import get_best_meetup_spot, get_central_point

class RecommendMeetupView(APIView):
    def post(self, request):
        participants = request.data.get('participants', [])
        if not participants:
            return Response({'error': 'Please provide at least one participant coordinate.'}, status=status.HTTP_400_BAD_REQUEST)
        
        best_match = get_best_meetup_spot(participants)
        center = get_central_point(participants)
        
        if not best_match:
            return Response({'error': 'Could not find a suitable meeting spot. Ensure API keys are correct and there are places nearby.'}, status=status.HTTP_404_NOT_FOUND)
            
        return Response({
            'best_match': best_match,
            'center': center,
            'participants': participants
        })
